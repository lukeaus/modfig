from __future__ import annotations

import json
import textwrap
from collections.abc import Iterator, Mapping
from pathlib import PurePosixPath
from typing import Any

import pytest

from modfig.adapters import (
    AbsentDestination,
    AdapterContext,
    AdapterMetadata,
    AdapterPlanContext,
    AdapterPlanError,
    AdapterV1,
    AdapterValidationContext,
    ArtifactIdentity,
    ResolvedModel,
)
from modfig.clients.factory import adapter, build_models, plan_factory
from modfig.errors import AppError
from modfig.registry import FactoryNativeReference, ModelReference, load_registry_text
from modfig.state import CollisionError

REGISTRY = textwrap.dedent(
    """\
    specVersion: "0.1"
    providers:
      router:
        name: Router
        targets: [factory]
        baseUrl: https://router.example/v1
        apiKey: env.ROUTER_KEY
        provider: generic-chat-completion-api
        enabled: true
        models:
          primary:
            displayName: Primary
            contextWindow: 8192
            maxOutputTokens: 1024
            favourite: true
            enabled: true
          routed:
            displayName: Routed
            contextWindow: 8192
            maxOutputTokens: 1024
            provider: OpenRouter
            enabled: true
    """
)


def _gpt_factory_registry(*, provider_protocol: str | None, model_name: str = "gpt-5") -> str:
    protocol_line = f"            provider: {provider_protocol}\n" if provider_protocol else ""
    return textwrap.dedent(
        f"""\
        specVersion: "0.1"
        providers:
          router:
            name: Router
            targets: [factory]
            baseUrl: https://router.example/v1
            apiKey: env.ROUTER_KEY
{protocol_line}            enabled: true
            models:
              {model_name}:
                displayName: GPT Model
                contextWindow: 8192
                maxOutputTokens: 1024
                enabled: true
        """
    )


class UnreadableSecrets(Mapping[str, str]):
    def __getitem__(self, key: str) -> str:
        raise AssertionError(f"secret read: {key}")

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0

    def get(self, key: str, default: Any = None) -> Any:
        raise AssertionError(f"secret read: {key}")


def _adapter_model(*, favourite: bool = True) -> ResolvedModel:
    return ResolvedModel(
        provider_key="router",
        base_url="https://router.example/v1",
        api_key_reference="env.ROUTER_KEY",
        model="primary",
        display_name="Primary",
        max_output_tokens=1024,
        effective_provider="generic-chat-completion-api",
        no_image_support=False,
        favourite=favourite,
        factory_id="custom:primary--router",
    )


def test_factory_builtin_adapter_plans_models_from_bounded_context() -> None:
    model = _adapter_model()
    context = AdapterPlanContext("factory", "core", {}, (model,), lambda reference: model)
    identity = ArtifactIdentity("factory-config", PurePosixPath("settings.json"))
    source = b'{"other":{"keep":true},"customModels":[{"id":"foreign"}],"modelFavorites":[]}'

    plan = adapter.plan(
        context,
        {identity: source},
        {"modelIds": ["custom:stale--router"], "favoriteIds": ["custom:stale--router"]},
    )

    assert len(plan.artifacts) == 1
    artifact = plan.artifacts[0]
    assert artifact.artifact == identity
    assert artifact.feature_key == "features.core.models"
    assert artifact.reconciliation == {
        "modelIds": ("custom:primary--router",),
        "favoriteIds": ("custom:primary--router",),
        "fields": (),
        "affectedModelIds": (),
    }
    assert plan.ownership == artifact.reconciliation
    settings = json.loads(artifact.planned)
    assert settings["other"] == {"keep": True}
    assert [entry["id"] for entry in settings["customModels"]] == [
        "foreign",
        "custom:primary--router",
    ]
    assert settings["customModels"][1]["apiKey"] == "${ROUTER_KEY}"
    assert settings["modelFavorites"] == ["custom:primary--router"]
    assert (
        artifact.planned
        == (json.dumps(settings, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    )


def test_factory_builtin_adapter_verifies_exact_written_snapshot() -> None:
    written = b'{"customModels":[],"modelFavorites":[]}\n'

    adapter.recheck()
    adapter.verify(AdapterContext("factory", "core"), (written,))

    with pytest.raises(AdapterPlanError, match="written snapshot"):
        adapter.verify(AdapterContext("factory", "core"), ())
    with pytest.raises(AdapterPlanError, match="written snapshot"):
        adapter.verify(AdapterContext("factory", "core"), (AbsentDestination(),))


def test_factory_builtin_adapter_rejects_missing_or_absent_snapshot() -> None:
    model = _adapter_model()
    context = AdapterPlanContext("factory", "core", {}, (model,), lambda reference: model)
    identity = ArtifactIdentity("factory-config", PurePosixPath("settings.json"))

    for snapshots in ({}, {identity: AbsentDestination()}):
        with pytest.raises(AdapterPlanError, match="snapshot.*missing|snapshot.*absent"):
            adapter.plan(context, snapshots, {})


def test_factory_builtin_adapter_projects_defaults_with_field_ownership() -> None:
    model = _adapter_model()
    defaults = {
        "worker": ModelReference("router", "primary"),
        "thinker": ModelReference("router", "primary"),
        "orchestrator": ModelReference("router", "primary"),
        "simple": ModelReference("router", "primary"),
        "validator": ModelReference("router", "primary"),
    }
    context = AdapterPlanContext(
        "factory", "core", {"defaults": defaults}, (model,), lambda reference: model
    )
    identity = ArtifactIdentity("factory-config", PurePosixPath("settings.json"))
    source = b'{"other":{"keep":true},"agents":{"foreign":{"keep":true}},"customModels":[]}'

    plan = adapter.plan(context, {identity: source}, {})

    assert len(plan.artifacts) == 1
    assert json.loads(plan.artifacts[0].planned) == {
        "other": {"keep": True},
        "agents": {
            "foreign": {"keep": True},
            "worker": {"model": "custom:primary--router"},
            "thinker": {"model": "custom:primary--router"},
            "orchestrator": {"model": "custom:primary--router"},
            "simple": {"model": "custom:primary--router"},
            "validator": {"model": "custom:primary--router"},
        },
        "customModels": [
            {
                "model": "primary",
                "id": "custom:primary--router",
                "baseUrl": "https://router.example/v1",
                "apiKey": "${ROUTER_KEY}",
                "displayName": "Primary",
                "maxOutputTokens": 1024,
                "noImageSupport": False,
                "provider": "generic-chat-completion-api",
            }
        ],
        "modelFavorites": ["custom:primary--router"],
    }
    fields = plan.ownership["fields"]
    assert len(fields) == 5
    assert {(field["logicalKey"], field["jsonPointer"]) for field in fields} == {
        ("defaults.worker", "/agents/worker/model"),
        ("defaults.thinker", "/agents/thinker/model"),
        ("defaults.orchestrator", "/agents/orchestrator/model"),
        ("defaults.simple", "/agents/simple/model"),
        ("defaults.validator", "/agents/validator/model"),
    }
    assert all(field["before"] == {"kind": "absent"} for field in fields)
    assert all(len(field["writtenSha256"]) == 64 for field in fields)


def test_factory_builtin_adapter_projects_session_and_mission_scalars() -> None:
    model = _adapter_model()
    context = AdapterPlanContext(
        "factory",
        "core",
        {
            "session": {
                "model": ModelReference("router", "primary"),
                "reasoningEffort": "high",
                "specModeModel": FactoryNativeReference("factory-native-spec"),
                "specModeReasoningEffort": "medium",
            },
            "mission": {
                "orchestratorModel": ModelReference("router", "primary"),
                "workerReasoningEffort": "low",
                "validationWorkerModel": FactoryNativeReference("factory-native-validator"),
            },
            "subagent": {
                "lightModel": ModelReference("router", "primary"),
                "mediumModel": ModelReference("router", "primary"),
                "heavyModel": FactoryNativeReference("factory-native-heavy"),
            },
        },
        (model,),
        lambda reference: model,
    )
    identity = ArtifactIdentity("factory-config", PurePosixPath("settings.json"))

    plan = adapter.plan(context, {identity: b'{"keep":true,"customModels":[]}'}, {})

    settings = json.loads(plan.artifacts[0].planned)
    assert settings["keep"] is True
    assert settings["session"] == {
        "model": "custom:primary--router",
        "reasoning": "high",
        "spec": {"model": "factory-native-spec", "reasoning": "medium"},
    }
    assert settings["sessionDefaultSettings"] == {
        "model": "custom:primary--router",
        "reasoningEffort": "high",
        "specModeModel": "factory-native-spec",
        "specModeReasoningEffort": "medium",
    }
    assert settings["mission"] == {
        "orchestrator": {"model": "custom:primary--router"},
        "worker": {"reasoning": "low"},
        "validation": {"model": "factory-native-validator"},
    }
    assert settings["missionOrchestratorModel"] == "custom:primary--router"
    assert settings["missionModelSettings"] == {
        "workerReasoningEffort": "low",
        "validationWorkerModel": "factory-native-validator",
    }
    assert settings["subagentModelSettings"] == {
        "lightModel": "custom:primary--router",
        "mediumModel": "custom:primary--router",
        "heavyModel": "factory-native-heavy",
    }
    assert {(field["logicalKey"], field["jsonPointer"]) for field in plan.ownership["fields"]} == {
        ("session.model", "/session/model"),
        ("session.reasoningEffort", "/session/reasoning"),
        ("session.specModeModel", "/session/spec/model"),
        ("session.specModeReasoningEffort", "/session/spec/reasoning"),
        ("session.defaultModel", "/sessionDefaultSettings/model"),
        ("session.defaultReasoningEffort", "/sessionDefaultSettings/reasoningEffort"),
        ("session.defaultSpecModeModel", "/sessionDefaultSettings/specModeModel"),
        (
            "session.defaultSpecModeReasoningEffort",
            "/sessionDefaultSettings/specModeReasoningEffort",
        ),
        ("mission.orchestratorModel", "/mission/orchestrator/model"),
        ("mission.workerReasoningEffort", "/mission/worker/reasoning"),
        ("mission.validationWorkerModel", "/mission/validation/model"),
        ("mission.defaultOrchestratorModel", "/missionOrchestratorModel"),
        (
            "mission.defaultWorkerReasoningEffort",
            "/missionModelSettings/workerReasoningEffort",
        ),
        (
            "mission.defaultValidationWorkerModel",
            "/missionModelSettings/validationWorkerModel",
        ),
        ("subagent.lightModel", "/subagentModelSettings/lightModel"),
        ("subagent.mediumModel", "/subagentModelSettings/mediumModel"),
        ("subagent.heavyModel", "/subagentModelSettings/heavyModel"),
    }


def test_factory_scalar_fields_reject_collisions_restore_owned_values_and_reject_drift() -> None:
    model = _adapter_model()
    context = AdapterPlanContext(
        "factory",
        "core",
        {"session": {"reasoningEffort": "high"}},
        (model,),
        lambda reference: model,
    )
    identity = ArtifactIdentity("factory-config", PurePosixPath("settings.json"))

    with pytest.raises(CollisionError):
        adapter.plan(context, {identity: b'{"session":{"reasoning":"low"},"customModels":[]}'}, {})

    initial = adapter.plan(context, {identity: b'{"session":{"keep":true},"customModels":[]}'}, {})
    owned = initial.ownership
    written = initial.artifacts[0].planned
    cleaned = adapter.plan(
        AdapterPlanContext("factory", "core", {}, (model,), lambda reference: model),
        {identity: written},
        owned,
    )
    assert json.loads(cleaned.artifacts[0].planned)["session"] == {"keep": True}
    assert cleaned.ownership["fields"] == ()

    drifted = json.loads(written)
    drifted["session"]["reasoning"] = "low"
    with pytest.raises(AdapterPlanError, match="drifted"):
        adapter.plan(context, {identity: json.dumps(drifted).encode()}, owned)


def test_factory_session_alias_migrates_when_primary_field_is_owned() -> None:
    model = _adapter_model()
    context = AdapterPlanContext(
        "factory",
        "core",
        {"session": {"model": ModelReference("router", "primary")}},
        (model,),
        lambda reference: model,
    )
    identity = ArtifactIdentity("factory-config", PurePosixPath("settings.json"))
    initial = adapter.plan(context, {identity: b'{"customModels":[]}'}, {})
    owned = dict(initial.ownership)
    owned["fields"] = tuple(
        field
        for field in owned["fields"]
        if not str(field["logicalKey"]).startswith("session.default")
    )
    written = json.loads(initial.artifacts[0].planned)
    written["sessionDefaultSettings"] = {"model": "custom:kimi-k3--openrouter"}

    migrated = adapter.plan(context, {identity: json.dumps(written).encode()}, owned)
    migrated_settings = json.loads(migrated.artifacts[0].planned)
    assert migrated_settings["sessionDefaultSettings"]["model"] == "custom:primary--router"
    alias_field = next(
        field
        for field in migrated.ownership["fields"]
        if field["logicalKey"] == "session.defaultModel"
    )
    assert alias_field["before"] == {
        "kind": "json",
        "value": "custom:kimi-k3--openrouter",
    }


def test_factory_scalar_features_coexist() -> None:
    model = _adapter_model()
    context = AdapterPlanContext(
        "factory",
        "core",
        {
            "defaults": {
                role: ModelReference("router", "primary")
                for role in ("worker", "thinker", "orchestrator", "simple", "validator")
            },
            "session": {"model": ModelReference("router", "primary")},
        },
        (model,),
        lambda reference: model,
    )
    identity = ArtifactIdentity("factory-config", PurePosixPath("settings.json"))

    plan = adapter.plan(context, {identity: b'{"customModels":[]}'}, {})

    assert {field["logicalKey"] for field in plan.ownership["fields"]} == {
        "defaults.worker",
        "defaults.thinker",
        "defaults.orchestrator",
        "defaults.simple",
        "defaults.validator",
        "session.model",
        "session.defaultModel",
    }


def test_factory_projections_emit_per_model_base_url_override() -> None:
    registry = load_registry_text(
        textwrap.dedent(
            """\
            specVersion: "0.1"
            providers:
              openrouter:
                name: OpenRouter
                targets: [factory]
                baseUrl: https://openrouter.ai/api/v1
                apiKey: env.OPEN_ROUTER_API_KEY
                provider: anthropic
                enabled: true
                models:
                  claude-sonnet-5:
                    displayName: Claude Sonnet 5
                    contextWindow: 1048576
                    maxOutputTokens: 128000
                    baseUrl: https://openrouter.ai/api/v1/anthropic
                    enabled: true
                  claude-opus-4-5:
                    displayName: Claude Opus 4.5
                    contextWindow: 1048576
                    maxOutputTokens: 128000
                    enabled: true
            """
        )
    )
    projected = build_models(
        registry,
        {"OPEN_ROUTER_API_KEY": "secret"},
        {"customModels": [], "modelFavorites": []},
    )
    by_model = {entry["model"]: entry for entry in projected}
    assert by_model["claude-sonnet-5"]["baseUrl"] == "https://openrouter.ai/api/v1/anthropic"
    assert by_model["claude-opus-4-5"]["baseUrl"] == "https://openrouter.ai/api/v1"


def test_factory_validate_defensively_rejects_native_defaults_and_invalid_reasoning() -> None:
    validation = AdapterValidationContext("factory", "core", lambda reference: _adapter_model())
    with pytest.raises(AdapterPlanError, match="defaults"):
        adapter.validate(
            {
                "defaults": {
                    role: FactoryNativeReference("native")
                    for role in ("worker", "thinker", "orchestrator", "simple", "validator")
                }
            },
            validation,
        )
    with pytest.raises(AdapterPlanError, match="reasoning"):
        adapter.validate({"mission": {"workerReasoningEffort": "invalid"}}, validation)


def _defaults_context() -> AdapterPlanContext:
    model = _adapter_model()
    return AdapterPlanContext(
        "factory",
        "core",
        {
            "defaults": {
                role: ModelReference("router", "primary")
                for role in ("worker", "thinker", "orchestrator", "simple", "validator")
            }
        },
        (model,),
        lambda reference: model,
    )


def test_factory_defaults_reject_exactly_four_legacy_roles() -> None:
    # VAL-CATALOG-008: the four legacy roles are no longer a complete set.
    validation = AdapterValidationContext("factory", "core", lambda reference: _adapter_model())
    legacy = {
        role: ModelReference("router", "primary")
        for role in ("worker", "thinker", "orchestrator", "simple")
    }
    with pytest.raises(AdapterPlanError, match="five"):
        adapter.validate({"defaults": legacy}, validation)


def test_factory_defaults_reject_foreign_scalar_parent() -> None:
    identity = ArtifactIdentity("factory-config", PurePosixPath("settings.json"))

    with pytest.raises(CollisionError):
        adapter.plan(_defaults_context(), {identity: b'{"agents":null,"customModels":[]}'}, {})

    with pytest.raises(CollisionError):
        adapter.plan(
            _defaults_context(),
            {identity: b'{"agents":{"worker":{"model":"foreign"}},"customModels":[]}'},
            {},
        )

    plan = adapter.plan(
        _defaults_context(),
        {identity: b'{"agents":{"worker":{"model":"custom:primary--router"}},"customModels":[]}'},
        {},
    )
    worker = next(
        field for field in plan.ownership["fields"] if field["logicalKey"] == "defaults.worker"
    )
    assert worker["before"] == {"kind": "json", "value": "custom:primary--router"}


def test_factory_defaults_restore_or_delete_owned_fields_and_reject_drift() -> None:
    identity = ArtifactIdentity("factory-config", PurePosixPath("settings.json"))
    initial = adapter.plan(
        _defaults_context(), {identity: b'{"agents":{"keep":true},"customModels":[]}'}, {}
    )
    owned = initial.ownership
    written = initial.artifacts[0].planned
    cleanup = adapter.plan(
        AdapterPlanContext(
            "factory", "core", {}, (_adapter_model(),), lambda reference: _adapter_model()
        ),
        {identity: written},
        owned,
    )
    settings = json.loads(cleanup.artifacts[0].planned)
    assert settings["agents"] == {
        "keep": True,
        "worker": {},
        "thinker": {},
        "orchestrator": {},
        "simple": {},
        "validator": {},
    }
    assert cleanup.ownership["fields"] == ()

    drifted = json.loads(written)
    drifted["agents"]["worker"]["model"] = "foreign"
    with pytest.raises(AdapterPlanError, match="drifted"):
        adapter.plan(
            _defaults_context(),
            {identity: json.dumps(drifted).encode()},
            owned,
        )


def test_factory_defaults_reject_malformed_field_ownership_but_accept_legacy_models() -> None:
    identity = ArtifactIdentity("factory-config", PurePosixPath("settings.json"))
    context = AdapterPlanContext(
        "factory", "core", {}, (_adapter_model(),), lambda reference: _adapter_model()
    )
    plan = adapter.plan(
        context,
        {identity: b'{"customModels":[]}'},
        {"modelIds": [], "favoriteIds": []},
    )
    assert plan.ownership["fields"] == ()

    with pytest.raises(AdapterPlanError, match="field record"):
        adapter.plan(
            context,
            {identity: b'{"customModels":[]}'},
            {"fields": [{"logicalKey": "defaults.worker"}]},
        )


def test_factory_builtin_adapter_contract_validates_binding_and_snapshot() -> None:
    context = AdapterContext("factory", "core")
    identity = ArtifactIdentity("factory-config", PurePosixPath("settings.json"))

    assert isinstance(adapter, AdapterV1)
    assert adapter.describe() == AdapterMetadata("modfig.factory", "factory", "core")
    declaration = adapter.preflight(context)
    assert declaration.proof_requirements == {}
    assert tuple(request.artifact for request in declaration.read_requests) == (identity,)
    assert tuple(write.artifact for write in declaration.prospective_writes) == (identity,)

    validation = AdapterValidationContext(
        "factory",
        "core",
        lambda reference: load_registry_text(REGISTRY).resolve_model(reference, "factory"),
    )
    adapter.validate({}, validation)
    with pytest.raises(AdapterPlanError, match="binding"):
        adapter.validate({}, AdapterValidationContext("vscode", "core", validation.resolve_model))
    with pytest.raises(AdapterPlanError, match="snapshot"):
        adapter.plan(
            AdapterPlanContext("factory", "core", {}),
            {},
            {},
        )


def test_build_models_emits_effective_provider_and_settings_shape_index_policy() -> None:
    registry = load_registry_text(REGISTRY)
    settings = {"customModels": [{"id": "foreign", "index": 0, "provider": "openai"}]}

    models = build_models(registry, UnreadableSecrets(), settings, start_index=4)

    assert models == (
        {
            "model": "primary",
            "id": "custom:primary--router",
            "index": 4,
            "baseUrl": "https://router.example/v1",
            "apiKey": "${ROUTER_KEY}",
            "displayName": "Primary",
            "maxOutputTokens": 1024,
            "noImageSupport": False,
            "provider": "generic-chat-completion-api",
        },
        {
            "model": "routed",
            "id": "custom:routed--router",
            "index": 5,
            "baseUrl": "https://router.example/v1",
            "apiKey": "${ROUTER_KEY}",
            "displayName": "Routed",
            "maxOutputTokens": 1024,
            "noImageSupport": False,
            "provider": "OpenRouter",
        },
    )


def test_build_models_emits_factory_providers_and_passthroughs() -> None:
    registry = load_registry_text(
        textwrap.dedent(
            """\
            specVersion: "0.1"
            providers:
              openrouter:
                name: OpenRouter
                targets: [factory]
                baseUrl: https://openrouter.ai/api/v1
                apiKey: env.OPEN_ROUTER_API_KEY
                provider: generic-chat-completion-api
                enabled: true
                models:
                  gpt-5-mini:
                    displayName: GPT-5.6 Sol [OpenRouter]
                    contextWindow: 1048576
                    maxOutputTokens: 128000
                    maxInputTokens: 920576
                    enabled: true
                    extensions:
                      factory:
                        providers: [openai]
                        extraArgs:
                          max_price_per_1m: 8.0
                  gpt-5.5:
                    displayName: GPT-5.5 [OpenRouter]
                    contextWindow: 1048576
                    maxOutputTokens: 128000
                    enabled: true
                    extensions:
                      factory:
                        extraArgs:
                          temperature: 0.2
                        extraHeaders:
                          X-Pin: static
                  gpt-5.4:
                    displayName: GPT-5.4 [OpenRouter]
                    contextWindow: 1048576
                    maxOutputTokens: 128000
                    enabled: true
                    extensions:
                      factory:
                        extraArgs: [1, two, {three: null}]
                        extraHeaders: static
            """
        )
    )
    models = build_models(registry, UnreadableSecrets(), {"customModels": []})
    by_model = {model["model"]: model for model in models}
    # the wire provider stays untouched; the allow-list is merged into the
    # request-body extraArgs as the OpenRouter provider pin array
    assert by_model["gpt-5-mini"]["provider"] == "generic-chat-completion-api"
    assert by_model["gpt-5-mini"]["extraArgs"] == {
        "max_price_per_1m": 8.0,
        "provider": ["openai"],
    }
    assert "extraHeaders" not in by_model["gpt-5-mini"]
    assert by_model["gpt-5.5"]["provider"] == "generic-chat-completion-api"
    assert by_model["gpt-5.5"]["extraArgs"] == {"temperature": 0.2}
    assert by_model["gpt-5.5"]["extraHeaders"] == {"X-Pin": "static"}
    # non-object passthrough shapes are emitted verbatim
    assert by_model["gpt-5.4"]["extraArgs"] == [1, "two", {"three": None}]
    assert by_model["gpt-5.4"]["extraHeaders"] == "static"


def test_build_models_does_not_read_secret_values() -> None:
    registry = load_registry_text(REGISTRY)
    sentinel = "do-not-leak-sentinel"

    models = build_models(
        registry,
        {"env.ROUTER_KEY": sentinel},
        {"customModels": []},
    )

    assert models[0]["apiKey"] == "${ROUTER_KEY}"
    assert sentinel not in repr(models)


def test_factory_plan_preserves_foreign_state_and_removes_stale_owned_entries() -> None:
    registry = load_registry_text(REGISTRY)
    settings = {
        "other": {"keep": True},
        "customModels": [
            {"id": "foreign", "model": "foreign", "index": 3},
            {"id": "custom:primary--router", "model": "primary", "apiKey": "old"},
            {"id": "custom:obsolete--router", "model": "obsolete"},
        ],
        "modelFavorites": ["foreign", "foreign", "custom:obsolete--router"],
    }

    plan = plan_factory(
        registry,
        settings,
        owned_model_ids={"custom:primary--router", "custom:obsolete--router"},
        owned_favorite_ids={"custom:obsolete--router"},
        secrets=UnreadableSecrets(),
    )

    assert plan.settings["other"] == {"keep": True}
    assert [model["id"] for model in plan.settings["customModels"]] == [
        "foreign",
        "custom:primary--router",
        "custom:routed--router",
    ]
    assert plan.settings["modelFavorites"] == ["foreign", "foreign", "custom:primary--router"]
    assert plan.owned_model_ids == frozenset({"custom:primary--router", "custom:routed--router"})
    assert plan.owned_favorite_ids == frozenset({"custom:primary--router"})


def test_factory_plan_exposes_ordered_affected_existing_managed_models() -> None:
    registry = load_registry_text(REGISTRY)
    settings = {
        "customModels": [
            {"id": "foreign", "model": "foreign"},
            {"id": "custom:routed--router", "model": "routed", "displayName": "old"},
            {"id": "custom:primary--router", "model": "primary", "displayName": "Primary"},
            {"id": "custom:stale--router", "model": "stale"},
        ],
        "modelFavorites": [],
    }

    plan = plan_factory(
        registry,
        settings,
        owned_model_ids={
            "custom:routed--router",
            "custom:primary--router",
            "custom:stale--router",
        },
        owned_favorite_ids=set(),
        secrets=UnreadableSecrets(),
    )

    assert plan.affected_model_ids == (
        "custom:routed--router",
        "custom:primary--router",
        "custom:stale--router",
    )


def test_factory_plan_excludes_favorite_only_and_scalar_only_model_changes() -> None:
    registry = load_registry_text(REGISTRY)
    model = {
        "model": "primary",
        "id": "custom:primary--router",
        "baseUrl": "https://router.example/v1",
        "apiKey": "${ROUTER_KEY}",
        "displayName": "Primary",
        "maxOutputTokens": 1024,
        "noImageSupport": False,
        "provider": "generic-chat-completion-api",
    }
    plan = plan_factory(
        registry,
        {"customModels": [model], "modelFavorites": ["foreign"]},
        owned_model_ids={"custom:primary--router"},
        owned_favorite_ids={"custom:primary--router"},
        secrets=UnreadableSecrets(),
    )
    assert plan.affected_model_ids == ()


def test_factory_plan_replaces_divergent_unowned_custom_model() -> None:
    registry = load_registry_text(REGISTRY)
    settings = {"customModels": [{"id": "custom:primary--router", "model": "foreign"}]}

    plan = plan_factory(
        registry,
        settings,
        owned_model_ids=set(),
        owned_favorite_ids=set(),
        secrets=UnreadableSecrets(),
    )

    assert [model["id"] for model in plan.settings["customModels"]] == [
        "custom:primary--router",
        "custom:routed--router",
    ]
    assert plan.settings["customModels"][0]["model"] == "primary"


def test_factory_plan_removes_stale_custom_models_and_favorites_without_ownership() -> None:
    registry = load_registry_text(REGISTRY)
    settings = {
        "customModels": [
            {"id": "foreign", "model": "foreign"},
            {"id": "custom:primary--router", "model": "old"},
            {"id": "custom:obsolete--router", "model": "obsolete"},
        ],
        "modelFavorites": ["foreign", "custom:obsolete--router"],
    }

    plan = plan_factory(
        registry,
        settings,
        owned_model_ids=set(),
        owned_favorite_ids=set(),
        secrets=UnreadableSecrets(),
    )

    assert [model["id"] for model in plan.settings["customModels"]] == [
        "foreign",
        "custom:primary--router",
        "custom:routed--router",
    ]
    assert plan.settings["modelFavorites"] == ["foreign", "custom:primary--router"]


def test_factory_plan_rejects_duplicate_existing_ids_before_reconciliation() -> None:
    registry = load_registry_text(REGISTRY)
    duplicate = "foreign"
    settings = {
        "customModels": [
            {"id": duplicate, "model": "first"},
            {"id": duplicate, "model": "second"},
        ]
    }

    with pytest.raises(AppError, match="duplicate.*foreign"):
        plan_factory(
            registry,
            settings,
            owned_model_ids=set(),
            owned_favorite_ids=set(),
            secrets=UnreadableSecrets(),
        )


def test_factory_plan_rejects_removing_active_owned_model() -> None:
    registry = load_registry_text(REGISTRY)
    settings = {
        "activeModel": "custom:obsolete--router",
        "customModels": [{"id": "custom:obsolete--router", "model": "obsolete"}],
    }

    with pytest.raises(AppError, match="active.*manual|manual.*active"):
        plan_factory(
            registry,
            settings,
            owned_model_ids={"custom:obsolete--router"},
            owned_favorite_ids=set(),
            secrets=UnreadableSecrets(),
        )


def test_factory_projection_emits_provider_for_openai_with_empty_settings() -> None:
    registry = load_registry_text(
        _gpt_factory_registry(provider_protocol="openai", model_name="gpt-5")
    )
    models = build_models(registry, UnreadableSecrets(), {"customModels": []})
    assert models[0]["provider"] == "openai"


def test_factory_projection_emits_provider_for_generic_with_empty_settings() -> None:
    registry = load_registry_text(REGISTRY)
    models = build_models(registry, UnreadableSecrets(), {"customModels": []})
    assert models[0]["provider"] == "generic-chat-completion-api"
    assert models[1]["provider"] == "OpenRouter"


@pytest.mark.parametrize("model_name", ["gpt-5", "gpt-4o", "o3", "codex-mini"])
def test_factory_projection_emits_declared_generic_transport_for_gpt_models(
    model_name: str,
) -> None:
    # VAL-CATALOG-006: a GPT-family model under a generic provider emits the
    # declared generic transport; no static name-based guard rejects it.
    registry = load_registry_text(
        _gpt_factory_registry(provider_protocol=None, model_name=model_name)
    )
    models = build_models(
        registry, UnreadableSecrets(), {"customModels": [{"id": "foreign", "provider": "openai"}]}
    )
    assert models[0]["model"] == model_name
    assert models[0]["provider"] == "generic-chat-completion-api"


@pytest.mark.parametrize("model_name", ["gpt-5", "gpt-4o", "o3", "codex-mini"])
def test_factory_projection_emits_declared_openai_transport_for_gpt_models(
    model_name: str,
) -> None:
    # VAL-CATALOG-006: the same GPT-family model under an openai provider emits
    # the declared openai transport.
    registry = load_registry_text(
        _gpt_factory_registry(provider_protocol="openai", model_name=model_name)
    )
    models = build_models(
        registry, UnreadableSecrets(), {"customModels": [{"id": "foreign", "provider": "openai"}]}
    )
    assert models[0]["provider"] == "openai"
