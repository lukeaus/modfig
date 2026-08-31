from __future__ import annotations

import hashlib
import http.client
import json
import re
import socket
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
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
    SnapshotRequest,
)
from ...errors import AppError
from ...registry import (
    REASONING_EFFORTS,
    FactoryNativeReference,
    Model,
    ModelReference,
    Provider,
    Registry,
)
from ...secrets import resolve_secret, secret_variable
from ...state import CollisionError, reconcile


@dataclass(frozen=True)
class FactoryShape:
    requires_index: bool


class _StrictJsonError(ValueError):
    pass


_FACTORY_ARTIFACT = ArtifactIdentity("factory-config", PurePosixPath("settings.json"))
_FACTORY_METADATA = AdapterMetadata("modfig.factory", "factory", "core")


@dataclass(frozen=True)
class FactoryAdapter:
    metadata: AdapterMetadata = _FACTORY_METADATA

    def describe(self) -> AdapterMetadata:
        return self.metadata

    def validate(self, config: Mapping[str, object], context: AdapterValidationContext) -> None:
        _validate_adapter_binding(context.logical_client, context.component)
        if not config:
            return
        _validate_factory_config(config)

    def preflight(self, context: AdapterContext) -> PreflightDeclaration:
        _validate_adapter_binding(context.logical_client, context.component)
        return PreflightDeclaration(
            {},
            (SnapshotRequest(_FACTORY_ARTIFACT),),
            (ProspectiveWrite(_FACTORY_ARTIFACT),),
        )

    def _scalar_plan_state(
        self,
        context: AdapterPlanContext,
        ownership: AdapterOwnership,
    ) -> tuple[dict[str, str], dict[str, str], tuple[dict[str, Any], ...]]:
        desired = _scalar_values(context.selected_config, context)
        ownership_fields = _ownership_fields(ownership)
        pointers = _scalar_pointers(_required_features(desired, ownership_fields))
        _validate_owned_field_pointers(ownership_fields, pointers)
        return desired, pointers, ownership_fields

    def plan(
        self,
        context: AdapterPlanContext,
        snapshots: Mapping[ArtifactIdentity, ArtifactSnapshot],
        ownership: AdapterOwnership,
    ) -> ArtifactPlan:
        _validate_adapter_binding(context.logical_client, context.component)
        source = snapshots.get(_FACTORY_ARTIFACT)
        if source is None or isinstance(source, AbsentDestination):
            raise AdapterPlanError("Factory settings snapshot is missing or absent")
        try:
            settings = _parse_factory_settings(source)
            desired, pointers, ownership_fields = self._scalar_plan_state(context, ownership)
            planned = plan_factory_models(
                context.models,
                settings,
                owned_model_ids=_ownership_ids(ownership, "modelIds"),
                owned_favorite_ids=_ownership_ids(ownership, "favoriteIds"),
                shape=_settings_shape(settings),
            )
            if pointers:
                planned = _plan_scalar_fields(planned, desired, pointers, ownership_fields)
            planned_bytes = _serialize_factory_settings(planned.settings)
        except AppError as exc:
            raise AdapterPlanError(exc.message) from exc
        reconciliation = {
            "modelIds": sorted(planned.owned_model_ids),
            "favoriteIds": sorted(planned.owned_favorite_ids),
            "fields": list(planned.fields),
            "affectedModelIds": list(planned.affected_model_ids),
        }
        return ArtifactPlan(
            (
                PlannedArtifact(
                    _FACTORY_ARTIFACT,
                    planned_bytes,
                    "features.core.models",
                    reconciliation,
                ),
            ),
            reconciliation,
        )

    def recheck(self) -> None:
        return None

    def verify(
        self,
        context: AdapterContext,
        written: Sequence[ArtifactSnapshot],
    ) -> None:
        _validate_adapter_binding(context.logical_client, context.component)
        if len(written) != 1 or not isinstance(written[0], bytes):
            raise AdapterPlanError("Factory verification requires one present written snapshot")
        try:
            _parse_factory_settings(written[0])
        except AppError as exc:
            raise AdapterPlanError(exc.message) from exc


def _validate_adapter_binding(logical_client: str, component: object) -> None:
    if logical_client != "factory" or component != "core":
        raise AdapterPlanError("Factory adapter binding must be factory/core")


adapter = FactoryAdapter()


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
class FactoryPlan:
    settings: dict[str, Any]
    owned_model_ids: frozenset[str]
    owned_favorite_ids: frozenset[str]
    fields: tuple[dict[str, Any], ...] = ()
    affected_model_ids: tuple[str, ...] = ()


_DEFAULT_ROLES = ("worker", "thinker", "orchestrator", "simple", "validator")
_SCALAR_FIELDS = (
    ("defaults", "worker", "features.core.defaults", "model"),
    ("defaults", "thinker", "features.core.defaults", "model"),
    ("defaults", "orchestrator", "features.core.defaults", "model"),
    ("defaults", "simple", "features.core.defaults", "model"),
    ("defaults", "validator", "features.core.defaults", "model"),
    ("session", "model", "features.core.session", "model"),
    ("session", "reasoningEffort", "features.core.session", "reasoning"),
    ("session", "specModeModel", "features.core.session", "model"),
    ("session", "specModeReasoningEffort", "features.core.session", "reasoning"),
    ("mission", "orchestratorModel", "features.core.mission", "model"),
    ("mission", "orchestratorReasoningEffort", "features.core.mission", "reasoning"),
    ("mission", "workerModel", "features.core.mission", "model"),
    ("mission", "workerReasoningEffort", "features.core.mission", "reasoning"),
    ("mission", "validationWorkerModel", "features.core.mission", "model"),
    ("mission", "validationWorkerReasoningEffort", "features.core.mission", "reasoning"),
    ("subagent", "lightModel", "features.core.subagent", "model"),
    ("subagent", "mediumModel", "features.core.subagent", "model"),
    ("subagent", "heavyModel", "features.core.subagent", "model"),
)
_SCALAR_FIELD_BY_KEY = {
    f"{section}.{name}": (section, name, feature, kind)
    for section, name, feature, kind in _SCALAR_FIELDS
}
_SCALAR_FIELD_BY_KEY.update(
    {
        "session.defaultModel": (
            "session",
            "model",
            "features.core.session",
            "model",
        ),
        "session.defaultReasoningEffort": (
            "session",
            "reasoningEffort",
            "features.core.session",
            "reasoning",
        ),
        "session.defaultSpecModeModel": (
            "session",
            "specModeModel",
            "features.core.session",
            "model",
        ),
        "session.defaultSpecModeReasoningEffort": (
            "session",
            "specModeReasoningEffort",
            "features.core.session",
            "reasoning",
        ),
        "mission.defaultOrchestratorModel": (
            "mission",
            "orchestratorModel",
            "features.core.mission",
            "model",
        ),
        "mission.defaultOrchestratorReasoningEffort": (
            "mission",
            "orchestratorReasoningEffort",
            "features.core.mission",
            "reasoning",
        ),
        "mission.defaultWorkerModel": (
            "mission",
            "workerModel",
            "features.core.mission",
            "model",
        ),
        "mission.defaultWorkerReasoningEffort": (
            "mission",
            "workerReasoningEffort",
            "features.core.mission",
            "reasoning",
        ),
        "mission.defaultValidationWorkerModel": (
            "mission",
            "validationWorkerModel",
            "features.core.mission",
            "model",
        ),
        "mission.defaultValidationWorkerReasoningEffort": (
            "mission",
            "validationWorkerReasoningEffort",
            "features.core.mission",
            "reasoning",
        ),
    }
)
_SCALAR_ALIAS_TO_PRIMARY = {
    "session.defaultModel": "session.model",
    "session.defaultReasoningEffort": "session.reasoningEffort",
    "session.defaultSpecModeModel": "session.specModeModel",
    "session.defaultSpecModeReasoningEffort": "session.specModeReasoningEffort",
    "mission.defaultOrchestratorModel": "mission.orchestratorModel",
    "mission.defaultOrchestratorReasoningEffort": "mission.orchestratorReasoningEffort",
    "mission.defaultWorkerModel": "mission.workerModel",
    "mission.defaultWorkerReasoningEffort": "mission.workerReasoningEffort",
    "mission.defaultValidationWorkerModel": "mission.validationWorkerModel",
    "mission.defaultValidationWorkerReasoningEffort": "mission.validationWorkerReasoningEffort",
}
_SCALAR_POINTERS = {
    **{f"defaults.{role}": f"/agents/{role}/model" for role in _DEFAULT_ROLES},
    "session.model": "/session/model",
    "session.reasoningEffort": "/session/reasoning",
    "session.specModeModel": "/session/spec/model",
    "session.specModeReasoningEffort": "/session/spec/reasoning",
    "session.defaultModel": "/sessionDefaultSettings/model",
    "session.defaultReasoningEffort": "/sessionDefaultSettings/reasoningEffort",
    "session.defaultSpecModeModel": "/sessionDefaultSettings/specModeModel",
    "session.defaultSpecModeReasoningEffort": ("/sessionDefaultSettings/specModeReasoningEffort"),
    "mission.orchestratorModel": "/mission/orchestrator/model",
    "mission.orchestratorReasoningEffort": "/mission/orchestrator/reasoning",
    "mission.workerModel": "/mission/worker/model",
    "mission.workerReasoningEffort": "/mission/worker/reasoning",
    "mission.validationWorkerModel": "/mission/validation/model",
    "mission.validationWorkerReasoningEffort": "/mission/validation/reasoning",
    "mission.defaultOrchestratorModel": "/missionOrchestratorModel",
    "mission.defaultOrchestratorReasoningEffort": "/missionOrchestratorReasoningEffort",
    "mission.defaultWorkerModel": "/missionModelSettings/workerModel",
    "mission.defaultWorkerReasoningEffort": "/missionModelSettings/workerReasoningEffort",
    "mission.defaultValidationWorkerModel": "/missionModelSettings/validationWorkerModel",
    "mission.defaultValidationWorkerReasoningEffort": (
        "/missionModelSettings/validationWorkerReasoningEffort"
    ),
    "subagent.lightModel": "/subagentModelSettings/lightModel",
    "subagent.mediumModel": "/subagentModelSettings/mediumModel",
    "subagent.heavyModel": "/subagentModelSettings/heavyModel",
}
_POINTER_TOKEN_RE = re.compile(r"(?:[^~/]|~[01])*")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


def _validate_settings(settings: object) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise AppError("Factory settings must be a JSON object")
    models = settings.get("customModels", [])
    if not isinstance(models, list) or not all(
        isinstance(item, dict) and isinstance(item.get("id"), str) for item in models
    ):
        raise AppError("Factory settings customModels must be a list of objects with string ids")
    favorites = settings.get("modelFavorites", [])
    if not isinstance(favorites, list) or not all(isinstance(item, str) for item in favorites):
        raise AppError("Factory settings modelFavorites must be a list of strings")
    return settings


def _parse_factory_settings(source: bytes) -> dict[str, Any]:
    try:
        settings = json.loads(
            source,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, _StrictJsonError):
        raise AppError("Factory settings contain malformed JSON") from None
    except json.JSONDecodeError as exc:
        raise AppError(f"Factory settings contain malformed JSON at byte {exc.pos}") from None
    return _validate_settings(settings)


def _ownership_ids(ownership: AdapterOwnership, key: str) -> frozenset[str]:
    value = ownership.get(key, ())
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise AdapterPlanError(f"Factory ownership {key} must be a list of strings")
    return frozenset(value)


def _validate_factory_config(config: Mapping[str, object]) -> None:
    if not config:
        return
    if not set(config) <= {"defaults", "session", "mission", "subagent"}:
        raise AdapterPlanError("Factory core has unsupported settings")
    if "defaults" in config:
        defaults = config["defaults"]
        if (
            not isinstance(defaults, Mapping)
            or set(defaults) != set(_DEFAULT_ROLES)
            or not all(isinstance(defaults[role], ModelReference) for role in _DEFAULT_ROLES)
        ):
            raise AdapterPlanError("Factory defaults must contain exactly five model references")
    for section, names in (
        ("session", {"model", "reasoningEffort", "specModeModel", "specModeReasoningEffort"}),
        (
            "mission",
            {
                "orchestratorModel",
                "orchestratorReasoningEffort",
                "workerModel",
                "workerReasoningEffort",
                "validationWorkerModel",
                "validationWorkerReasoningEffort",
            },
        ),
        (
            "subagent",
            {
                "lightModel",
                "mediumModel",
                "heavyModel",
            },
        ),
    ):
        if section not in config:
            continue
        value = config[section]
        if not isinstance(value, Mapping) or not value or not set(value) <= names:
            raise AdapterPlanError(f"Factory {section} settings are malformed")
        for name, selection in value.items():
            if name.endswith("Model") or name == "model":
                if not isinstance(selection, (ModelReference, FactoryNativeReference)):
                    raise AdapterPlanError(f"Factory {section}.{name} must be a model reference")
            elif not isinstance(selection, str) or selection not in REASONING_EFFORTS:
                raise AdapterPlanError(f"Factory {section}.{name} has invalid reasoning effort")


def _scalar_values(config: Mapping[str, object], context: AdapterPlanContext) -> dict[str, str]:
    _validate_factory_config(config)
    result: dict[str, str] = {}
    for section, name, _feature, kind in _SCALAR_FIELDS:
        section_values = config.get(section)
        if not isinstance(section_values, Mapping) or name not in section_values:
            continue
        value = section_values[name]
        if kind == "reasoning":
            assert isinstance(value, str)
            result[f"{section}.{name}"] = value
        elif isinstance(value, ModelReference):
            result[f"{section}.{name}"] = context.resolve_model(value).factory_id
        elif section != "defaults" and isinstance(value, FactoryNativeReference):
            result[f"{section}.{name}"] = value.identifier
        else:
            raise AdapterPlanError(f"Factory {section}.{name} must be a model reference")
    return result


def _pointer_tokens(pointer: object) -> tuple[str, ...]:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise AdapterPlanError("Factory field pointer must be a non-root RFC 6901 JSON pointer")
    tokens = pointer[1:].split("/")
    if not tokens or any(not _POINTER_TOKEN_RE.fullmatch(token) for token in tokens):
        raise AdapterPlanError("Factory field pointer is malformed")
    return tuple(token.replace("~1", "/").replace("~0", "~") for token in tokens)


def _required_features(
    desired: Mapping[str, str], ownership_fields: Sequence[Mapping[str, Any]]
) -> set[str]:
    return {
        _SCALAR_FIELD_BY_KEY[key][2]
        for key in (*desired, *(str(field["logicalKey"]) for field in ownership_fields))
    }


def _scalar_pointers(required: set[str]) -> dict[str, str]:
    return {
        key: _SCALAR_POINTERS[key]
        for key, (_section, _name, feature, _kind) in _SCALAR_FIELD_BY_KEY.items()
        if feature in required
    }


def _canonical_sha256(value: object) -> str:
    try:
        payload = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError):
        raise AdapterPlanError("Factory field value must be strict JSON") from None
    return hashlib.sha256(payload.encode()).hexdigest()


def _ownership_fields(ownership: AdapterOwnership) -> tuple[dict[str, Any], ...]:
    raw = ownership.get("fields", ())
    if not isinstance(raw, (list, tuple)):
        raise AdapterPlanError("Factory ownership fields must be a list")
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in raw:
        if not isinstance(record, Mapping) or set(record) != {
            "logicalKey",
            "jsonPointer",
            "before",
            "writtenSha256",
        }:
            raise AdapterPlanError("Factory ownership field record is malformed")
        key, pointer, before, written = (
            record["logicalKey"],
            record["jsonPointer"],
            record["before"],
            record["writtenSha256"],
        )
        if (
            not isinstance(key, str)
            or key not in _SCALAR_FIELD_BY_KEY
            or not isinstance(pointer, str)
            or not isinstance(before, Mapping)
            or not isinstance(written, str)
            or not _DIGEST_RE.fullmatch(written)
            or key in seen
        ):
            raise AdapterPlanError("Factory ownership field record is malformed")
        _pointer_tokens(pointer)
        if set(before) == {"kind"} and before["kind"] == "absent":
            normalized_before: dict[str, Any] = {"kind": "absent"}
        elif set(before) == {"kind", "value"} and before["kind"] == "json":
            _canonical_sha256(before["value"])
            normalized_before = {"kind": "json", "value": before["value"]}
        else:
            raise AdapterPlanError("Factory ownership field preimage is malformed")
        seen.add(key)
        fields.append(
            {
                "logicalKey": key,
                "jsonPointer": pointer,
                "before": normalized_before,
                "writtenSha256": written,
            }
        )
    return tuple(fields)


def _validate_owned_field_pointers(
    ownership_fields: Sequence[Mapping[str, Any]], pointers: Mapping[str, str]
) -> None:
    for field in ownership_fields:
        key = str(field["logicalKey"])
        if pointers.get(key) != field["jsonPointer"]:
            raise AdapterPlanError(
                "Factory ownership field pointer disagrees with the settings shape"
            )


def _read_pointer(settings: Mapping[str, Any], pointer: str) -> tuple[bool, Any]:
    current: Any = settings
    for token in _pointer_tokens(pointer):
        if not isinstance(current, Mapping) or token not in current:
            return False, None
        current = current[token]
    return True, current


def _write_pointer(settings: dict[str, Any], pointer: str, value: Any) -> None:
    tokens = _pointer_tokens(pointer)
    current: dict[str, Any] = settings
    for token in tokens[:-1]:
        if token not in current:
            child: Any = {}
            current[token] = child
        else:
            child = current[token]
        if not isinstance(child, dict):
            raise CollisionError(pointer)
        current = child
    current[tokens[-1]] = value


def _delete_pointer(settings: dict[str, Any], pointer: str) -> None:
    tokens = _pointer_tokens(pointer)
    current: Any = settings
    for token in tokens[:-1]:
        if not isinstance(current, dict) or token not in current:
            return
        current = current[token]
    if isinstance(current, dict):
        current.pop(tokens[-1], None)


def _plan_scalar_fields(
    plan: FactoryPlan,
    desired: Mapping[str, str],
    pointers: Mapping[str, str],
    ownership_fields: Sequence[Mapping[str, Any]],
) -> FactoryPlan:
    settings = json.loads(json.dumps(plan.settings, ensure_ascii=False, allow_nan=False))
    _validate_owned_field_pointers(ownership_fields, pointers)
    owned = {str(field["logicalKey"]): field for field in ownership_fields}
    fields: list[dict[str, Any]] = []
    for key in _SCALAR_FIELD_BY_KEY:
        pointer = pointers.get(key)
        existing = owned.get(key)
        if pointer is None:
            if existing is not None:
                raise AdapterPlanError(
                    "Factory ownership field pointer disagrees with the settings shape"
                )
            continue
        source_key = _SCALAR_ALIAS_TO_PRIMARY.get(key)
        desired_key = key if key in desired else source_key
        if desired_key is None or desired_key not in desired:
            if existing is None:
                continue
            present, current = _read_pointer(settings, pointer)
            if not present or _canonical_sha256(current) != existing["writtenSha256"]:
                raise AdapterPlanError("Factory owned field has drifted")
            before = existing["before"]
            if before["kind"] == "absent":
                _delete_pointer(settings, pointer)
            else:
                _write_pointer(settings, pointer, before["value"])
            continue
        value = desired[desired_key]
        present, current = _read_pointer(settings, pointer)
        if existing is not None:
            if not present or _canonical_sha256(current) != existing["writtenSha256"]:
                raise AdapterPlanError("Factory owned field has drifted")
            before = existing["before"]
        else:
            if present and current != value:
                primary = owned.get(source_key) if source_key is not None else None
                primary_pointer = pointers.get(source_key) if source_key is not None else None
                primary_present, primary_current = (
                    _read_pointer(settings, primary_pointer)
                    if primary_pointer is not None
                    else (False, None)
                )
                if (
                    source_key is None
                    or primary is None
                    or primary_pointer is None
                    or not primary_present
                    or _canonical_sha256(primary_current) != primary["writtenSha256"]
                ):
                    raise CollisionError(pointer)
            before = {"kind": "json", "value": current} if present else {"kind": "absent"}
        _write_pointer(settings, pointer, value)
        fields.append(
            {
                "logicalKey": key,
                "jsonPointer": pointer,
                "before": before,
                "writtenSha256": _canonical_sha256(value),
            }
        )
    return FactoryPlan(
        plan.settings if settings == plan.settings else settings,
        plan.owned_model_ids,
        plan.owned_favorite_ids,
        tuple(fields),
        plan.affected_model_ids,
    )


def _settings_shape(settings: Mapping[str, Any]) -> FactoryShape:
    models = _validate_settings(dict(settings))["customModels"]
    return FactoryShape(requires_index=any("index" in item for item in models))


def _serialize_factory_settings(settings: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                _validate_settings(dict(settings)), indent=2, ensure_ascii=False, allow_nan=False
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError):
        raise AppError("Factory settings cannot be serialized as strict UTF-8 JSON") from None


def build_model_snapshots(
    models_snapshot: Sequence[ResolvedModel],
    shape: FactoryShape,
    *,
    start_index: int = 0,
) -> tuple[dict[str, Any], ...]:
    """Project client-filtered model DTOs without Registry or secret access."""
    models: list[dict[str, Any]] = []
    for position, model in enumerate(models_snapshot):
        projected: dict[str, Any] = {
            "model": model.model,
            "id": model.factory_id,
            "baseUrl": model.resolved_base_url(),
            "apiKey": f"${{{secret_variable(model.api_key_reference)}}}",
            "displayName": model.display_name,
            "maxOutputTokens": model.max_output_tokens,
            "noImageSupport": model.no_image_support,
        }
        if shape.requires_index:
            projected["index"] = start_index + position
        projected["provider"] = model.effective_provider
        if model.factory_extra_args is not None:
            projected["extraArgs"] = model.factory_extra_args
        if model.factory_extra_headers is not None:
            projected["extraHeaders"] = model.factory_extra_headers
        models.append(projected)
    return tuple(models)


def plan_factory_models(
    models_snapshot: Sequence[ResolvedModel],
    settings: Mapping[str, Any],
    *,
    owned_model_ids: set[str] | frozenset[str],
    owned_favorite_ids: set[str] | frozenset[str],
    shape: FactoryShape,
) -> FactoryPlan:
    """Replace the managed custom-model projection while preserving other settings."""
    existing_models = _validate_settings(dict(settings)).get("customModels", [])
    model_ids = [item["id"] for item in existing_models]
    duplicate = next((item for item in model_ids if model_ids.count(item) > 1), None)
    if duplicate is not None:
        raise AppError(f"Factory settings contain duplicate model id {duplicate!r}")
    # Factory's customModels collection is ModFig's projection surface. Older
    # converters wrote the same deterministic custom IDs without a ModFig
    # manifest, so every custom: entry is replaceable during reconciliation.
    managed_model_ids = set(owned_model_ids) | {
        item["id"] for item in existing_models if item["id"].startswith("custom:")
    }
    foreign_indices = [
        item["index"]
        for item in existing_models
        if item.get("id") not in managed_model_ids and type(item.get("index")) is int
    ]
    generated = build_model_snapshots(
        models_snapshot, shape, start_index=max(foreign_indices, default=-1) + 1
    )
    generated_ids = frozenset(model["id"] for model in generated)
    active_model = settings.get("activeModel")
    if active_model in managed_model_ids and active_model not in generated_ids:
        raise AppError(
            f"Factory active model {active_model!r} would be removed; manual selection required"
        )
    merged_models = reconcile(
        existing_models, generated, managed_model_ids, lambda item: item["id"]
    )
    generated_favorites = tuple(model.factory_id for model in models_snapshot if model.favourite)
    managed_favorite_ids = set(owned_favorite_ids) | {
        favorite
        for favorite in settings.get("modelFavorites", [])
        if favorite.startswith("custom:")
    }
    merged_favorites = _merge_favorites(
        settings.get("modelFavorites", []), generated_favorites, managed_favorite_ids
    )
    planned_settings = dict(settings)
    planned_settings["customModels"] = list(merged_models)
    planned_settings["modelFavorites"] = list(merged_favorites)
    generated_by_id = {item["id"]: item for item in generated}
    affected = tuple(
        item["id"]
        for item in existing_models
        if item["id"] in managed_model_ids
        and (item["id"] not in generated_by_id or item != generated_by_id[item["id"]])
    )
    return FactoryPlan(
        planned_settings,
        generated_ids,
        frozenset(generated_favorites),
        affected_model_ids=affected,
    )


def _registry_model_snapshots(registry: Registry) -> tuple[ResolvedModel, ...]:
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
            vscode_id=model.vscode_id(),
            max_input_tokens=model.max_input_tokens,
            tool_calling=model.tool_calling,
            provider_name=provider.name,
            factory_extra_args=model.factory_extra_args(),
            factory_extra_headers=model.factory_extra_headers(),
            vscode_extra_args=model.vscode_extra_args(),
            vscode_extra_headers=model.vscode_extra_headers(),
            chatgpt_http_headers=provider.chatgpt_http_headers(),
            base_url_override=model.base_url_override,
        )
        for provider, model in registry.emitted_models("factory")
    )


def build_models(
    registry: Registry,
    secrets: Mapping[str, str],
    settings: Mapping[str, Any],
    *,
    start_index: int = 0,
) -> tuple[dict[str, Any], ...]:
    """Project enabled Factory models without reading resolved credentials."""
    del secrets
    return build_model_snapshots(
        _registry_model_snapshots(registry), _settings_shape(settings), start_index=start_index
    )


def plan_factory(
    registry: Registry,
    settings: Mapping[str, Any],
    *,
    owned_model_ids: set[str] | frozenset[str],
    owned_favorite_ids: set[str] | frozenset[str],
    secrets: Mapping[str, str],
) -> FactoryPlan:
    """Replace custom Factory records while preserving non-custom settings."""
    del secrets
    return plan_factory_models(
        _registry_model_snapshots(registry),
        settings,
        owned_model_ids=owned_model_ids,
        owned_favorite_ids=owned_favorite_ids,
        shape=_settings_shape(settings),
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        del req, fp, code, msg, headers, newurl
        return None


def probe_factory_models(
    registry: Registry,
    environ: Mapping[str, str],
    *,
    timeout: float = 120.0,
) -> tuple[tuple[str, str], ...]:
    """Probe each enabled Factory target's live wire transports.

    Fail-closed preflight used only by ``validate --adapters`` and ``apply``
    preflight. Missing secret, transport error, timeout, non-200, or unusable
    output raises ``AppError`` naming the provider/model and failure class.
    Credentials and response bodies are never included in error text.

    ``openai`` models are probed at ``<resolvedBaseUrl>/responses``.
    ``anthropic`` models are probed at ``<baseUrl>/v1/messages`` only when the
    model declares an explicit per-model ``baseUrl`` (the endpoint is asserted
    at the declared URL, e.g. a Surplus Anthropic endpoint). Anthropic models
    without an override stay unprobed: the provider-level endpoint is not
    claimed to serve Messages.

    ``MODFIG_PROBE_TIMEOUT`` overrides the per-request timeout (seconds), for
    providers whose cold starts exceed the default.
    """
    configured = environ.get("MODFIG_PROBE_TIMEOUT")
    if configured is not None:
        try:
            timeout = float(configured)
        except ValueError as exc:
            raise AppError("MODFIG_PROBE_TIMEOUT must be a positive number of seconds") from exc
        if timeout <= 0:
            raise AppError("MODFIG_PROBE_TIMEOUT must be a positive number of seconds")
    targets = tuple(
        (provider, model)
        for provider, model in registry.emitted_models("factory")
        if model.effective_provider == "openai"
        or (model.effective_provider == "anthropic" and model.base_url_override is not None)
    )
    if not targets:
        return ()
    probed: list[tuple[str, str]] = []
    for provider, model in targets:
        if model.effective_provider == "openai":
            _probe_responses_one(provider, model, environ, timeout=timeout)
        else:
            _probe_messages_one(provider, model, environ, timeout=timeout)
        probed.append((provider.key, model.model))
    return tuple(probed)


def _probe_responses_one(
    provider: Provider, model: Model, environ: Mapping[str, str], *, timeout: float
) -> None:
    identity = f"provider {provider.key!r} model {model.model!r} ({model.factory_id(provider.key)})"
    try:
        api_key = resolve_secret(provider.api_key_reference, environ)
    except AppError as exc:
        raise AppError(f"Responses probe failed for {identity}: {exc.message}") from None
    url = f"{provider.resolved_base_url(model).rstrip('/')}/responses"
    payload = json.dumps({"model": model.model, "input": "ping"}, ensure_ascii=False).encode(
        "utf-8"
    )
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as exc:
        failure = "redirect" if 300 <= exc.code < 400 else "non-200 response"
        detail = f"{failure} (status {exc.code})"
        raise AppError(f"Responses probe failed for {identity}: {detail}") from None
    except http.client.HTTPException:
        raise AppError(f"Responses probe failed for {identity}: transport error") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise AppError(f"Responses probe failed for {identity}: request timed out") from None
        raise AppError(f"Responses probe failed for {identity}: transport error") from None
    except TimeoutError:
        raise AppError(f"Responses probe failed for {identity}: request timed out") from None
    except OSError:
        raise AppError(f"Responses probe failed for {identity}: transport error") from None
    if status != 200:
        raise AppError(
            f"Responses probe failed for {identity}: non-200 response (status {status})"
        ) from None
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        raise AppError(f"Responses probe failed for {identity}: unusable response output") from None
    output = data.get("output") if isinstance(data, dict) else None
    # ponytail: a non-empty `output` list is the minimal usable-output signal;
    # deeper content/text checks would risk false negatives as the API evolves.
    if not isinstance(output, list) or not output:
        raise AppError(f"Responses probe failed for {identity}: unusable response output") from None


def _probe_messages_one(
    provider: Provider, model: Model, environ: Mapping[str, str], *, timeout: float
) -> None:
    identity = f"provider {provider.key!r} model {model.model!r} ({model.factory_id(provider.key)})"
    base_url = provider.resolved_base_url(model)
    assert model.base_url_override is not None, "messages probe requires an explicit override"
    try:
        api_key = resolve_secret(provider.api_key_reference, environ)
    except AppError as exc:
        raise AppError(f"Messages probe failed for {identity}: {exc.message}") from None
    url = f"{base_url.rstrip('/')}/v1/messages"
    payload = json.dumps(
        {
            "model": model.model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as exc:
        failure = "redirect" if 300 <= exc.code < 400 else "non-200 response"
        detail = f"{failure} (status {exc.code})"
        raise AppError(f"Messages probe failed for {identity}: {detail}") from None
    except http.client.HTTPException:
        raise AppError(f"Messages probe failed for {identity}: transport error") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise AppError(f"Messages probe failed for {identity}: request timed out") from None
        raise AppError(f"Messages probe failed for {identity}: transport error") from None
    except TimeoutError:
        raise AppError(f"Messages probe failed for {identity}: request timed out") from None
    except OSError:
        raise AppError(f"Messages probe failed for {identity}: transport error") from None
    if status != 200:
        raise AppError(
            f"Messages probe failed for {identity}: non-200 response (status {status})"
        ) from None
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        raise AppError(f"Messages probe failed for {identity}: unusable response output") from None
    # ponytail: a non-empty `content` array is the minimal usable-output
    # signal, mirroring the Responses probe; streaming-style delta payloads
    # without a content array are not accepted.
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, list) or not content:
        raise AppError(f"Messages probe failed for {identity}: unusable response output") from None


def _merge_favorites(
    existing: list[str], generated: tuple[str, ...], owned: set[str] | frozenset[str]
) -> tuple[str, ...]:
    desired = set(generated)
    merged: list[str] = []
    for favorite in existing:
        if favorite in owned and favorite not in desired:
            continue
        if favorite in desired:
            merged.append(favorite)
            desired.remove(favorite)
        else:
            merged.append(favorite)
    merged.extend(favorite for favorite in generated if favorite in desired)
    return tuple(merged)
