from __future__ import annotations

import textwrap
from collections.abc import Mapping
from pathlib import Path

import pytest

from modfig.adapters import (
    AdapterContext,
    AdapterPlanContext,
    AdapterPlanError,
    AdapterValidationContext,
    ResolvedModel,
    RuntimeProof,
)
from modfig.clients.vscode import (
    VSCodeRuntime,
    adapter,
    plan_vscode,
    plan_vscode_models,
    preflight,
    project_vscode_model_snapshots,
    project_vscode_providers,
)
from modfig.errors import AppError
from modfig.registry import load_registry_text
from modfig.state import CollisionError

REGISTRY = textwrap.dedent(
    """\
    specVersion: "0.1"
    providers:
      router:
        name: Router
        targets: [vscode]
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
            enabled: true
      foreign-only:
        name: Foreign Only
        targets: [factory]
        baseUrl: https://foreign.example/v1
        apiKey: env.FOREIGN_KEY
        provider: generic-chat-completion-api
        enabled: true
        models:
          foreign-model:
            displayName: Foreign
            contextWindow: 8192
            maxOutputTokens: 1024
            enabled: true
    """
)


def proven_runtime(
    tmp_path: Path | None = None,
    *,
    vendor_mapping: bool = False,
    vendor_map: Mapping[str, tuple[str, str]] | None = None,
) -> VSCodeRuntime:
    holder: dict[str, VSCodeRuntime] = {}
    root = tmp_path if tmp_path is not None else Path.cwd() / ".modfig-test" / "User"
    runtime = VSCodeRuntime(
        supported_os=("macos", "linux", "windows"),
        supported_channels=("stable", "insiders"),
        supported_profile_modes=("default", "profile", "portable"),
        user_data_root=root,
        settings_path=root / "chatLanguageModels.json",
        state_db_path=root / "state.vscdb",
        state_wal_path=root / "state.vscdb-wal",
        state_shm_path=root / "state.vscdb-shm",
        safe_storage_supported=True,
        key_context="proven",
        process_quiescent=True,
        vendor_api_type_mapping=vendor_mapping,
        vendor_api_type_map=vendor_map or {},
        runtime_recheck=lambda: True,
        runtime_probe=lambda: holder["runtime"],
        os_name="linux",
        channel="stable",
        profile_mode="default",
        secret_format="oscrypt-v11",
    )
    holder["runtime"] = runtime
    return runtime


def test_vscode_builtin_adapter_contract_declares_stable_bundle() -> None:
    context = AdapterContext("vscode", "core")
    declaration = adapter.preflight(context)
    assert declaration.proof_requirements["runtimeProof"] == (
        "stable-code-quiescence-and-secret-contract"
    )
    assert tuple(str(request.artifact.relative_path) for request in declaration.read_requests) == (
        "chatLanguageModels.json",
        "state.vscdb",
        "state.vscdb-wal",
        "state.vscdb-shm",
    )


def test_vscode_builtin_adapter_rejects_missing_runtime_proof() -> None:
    validation = AdapterValidationContext(
        "vscode",
        "core",
        lambda reference: load_registry_text(REGISTRY).resolve_model(reference, "vscode"),
    )
    adapter.validate({}, validation)
    with pytest.raises(AdapterPlanError, match="runtime proof"):
        adapter.plan(
            AdapterPlanContext("vscode", "core", {}), RuntimeProof({}, "declaration"), {}, {}
        )


def test_preflight_fails_closed_with_exit_1() -> None:
    with pytest.raises(AppError) as exc_info:
        preflight()
    assert exc_info.value.exit_code == 1


def test_preflight_message_names_unavailable_until_proof_of_life() -> None:
    with pytest.raises(AppError) as exc_info:
        preflight()
    message = exc_info.value.message.lower()
    assert "vs code" in message
    assert "unavailable" in message
    assert "proof-of-life" in message
    assert "macos" in message
    assert "linux" in message


# --- Category 4: projection never reads sentinel secrets ---


def test_projection_emits_env_key_references_without_reading_secrets() -> None:
    registry = load_registry_text(REGISTRY)
    sentinel = "do-not-leak-sentinel"
    runtime = proven_runtime()

    providers = project_vscode_providers(registry, runtime)

    assert providers
    router = next(p for p in providers if p["id"] == "ModFig/router")
    assert router["id"] == "ModFig/router"
    assert router["baseUrl"] == "https://router.example/v1"
    model_ids = [m["id"] for m in router["models"]]
    assert model_ids == ["primary", "routed"]
    assert sentinel not in repr(providers)


def test_projection_omits_vendor_api_type_when_mapping_unproven() -> None:
    registry = load_registry_text(REGISTRY)
    runtime = proven_runtime(vendor_mapping=False)

    providers = project_vscode_providers(registry, runtime)

    router = providers[0]
    assert "vendor" not in router
    assert "apiType" not in router
    for model in router["models"]:
        assert "vendor" not in model
        assert "apiType" not in model


def test_projection_includes_vendor_api_type_only_from_proof_map() -> None:
    registry = load_registry_text(REGISTRY)
    runtime = proven_runtime(
        vendor_mapping=True,
        vendor_map={"router": ("openai", "openai")},
    )

    providers = project_vscode_providers(registry, runtime)

    router = providers[0]
    assert router["vendor"] == "openai"
    assert router["apiType"] == "openai"


def test_projection_rejects_proven_mapping_with_missing_provider_entry() -> None:
    registry = load_registry_text(REGISTRY)
    runtime = proven_runtime(vendor_mapping=True, vendor_map={})

    with pytest.raises(AppError, match="vendor|apiType"):
        project_vscode_providers(registry, runtime)


def test_projection_does_not_leak_registry_provider_protocol_as_vendor() -> None:
    registry = load_registry_text(REGISTRY)
    runtime = proven_runtime(
        vendor_mapping=True,
        vendor_map={"router": ("proof-vendor", "proof-api")},
    )

    providers = project_vscode_providers(registry, runtime)

    router = providers[0]
    assert router["vendor"] == "proof-vendor"
    assert router["vendor"] != "generic-chat-completion-api"
    assert router["apiType"] == "proof-api"


def test_current_code_projection_matches_working_vscode_writer_schema() -> None:
    registry = load_registry_text(REGISTRY)
    plan = plan_vscode(
        registry,
        [{"name": "Built-in", "settings": {"gpt-5.4": {}}}],
        owned_provider_ids=set(),
        owned_model_ids={},
        runtime=proven_runtime(),
    )

    router = next(provider for provider in plan.settings if provider["name"] == "Router")
    assert set(router) == {"name", "vendor", "apiKey", "apiType", "models", "settings"}
    assert router["apiKey"] == "${input:chat.lm.secret.lm-router}"
    assert router["vendor"] == "customendpoint"
    assert router["apiType"] == "chat-completions"
    assert router["models"][0] == {
        "id": "primary",
        "name": "Primary",
        "url": "https://router.example/v1/chat/completions",
        "toolCalling": True,
        "vision": True,
        "maxInputTokens": 7168,
        "maxOutputTokens": 1024,
    }


def test_current_code_projection_emits_reasoning_controls() -> None:
    model = ResolvedModel(
        provider_key="router",
        base_url="https://router.example/v1",
        api_key_reference="env.ROUTER_KEY",
        model="primary",
        display_name="Primary",
        max_output_tokens=1024,
        effective_provider="generic-chat-completion-api",
        no_image_support=False,
        favourite=False,
        factory_id="custom:primary--router",
        vscode_id="primary",
        vscode_reasoning_levels=("low", "medium", "high", "xhigh", "max"),
    )

    projected = project_vscode_model_snapshots((model,), proven_runtime())

    assert projected[0]["settings"] == {"primary": {}}
    assert projected[0]["models"][0]["supportsReasoningEffort"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]


def test_current_code_projection_emits_model_options_and_request_headers() -> None:
    # VS Code's chatLanguageModels contract renders request passthroughs as
    # modelOptions (body) and requestHeaders (headers).
    model = ResolvedModel(
        provider_key="router",
        base_url="https://router.example/v1",
        api_key_reference="env.ROUTER_KEY",
        model="primary",
        display_name="Primary",
        max_output_tokens=1024,
        effective_provider="generic-chat-completion-api",
        no_image_support=False,
        favourite=False,
        factory_id="custom:primary--router",
        vscode_id="primary",
        vscode_extra_args={"temperature": 0.2},
        vscode_extra_headers={"X-Custom": "static-value"},
    )

    projected = project_vscode_model_snapshots((model,), proven_runtime())

    assert projected[0]["models"][0]["modelOptions"] == {"temperature": 0.2}
    assert projected[0]["models"][0]["requestHeaders"] == {"X-Custom": "static-value"}


def test_vscode_extension_passthroughs_reach_projected_settings() -> None:
    # registry-driven: extensions.vscode.extraArgs/extraHeaders validate and
    # flow through the plan into modelOptions/requestHeaders
    registry = load_registry_text(
        textwrap.dedent(
            """\
            specVersion: "0.1"
            providers:
              router:
                name: Router
                targets: [vscode]
                baseUrl: https://router.example/v1
                apiKey: env.ROUTER_KEY
                provider: generic-chat-completion-api
                enabled: true
                models:
                  primary:
                    displayName: Primary
                    contextWindow: 8192
                    maxOutputTokens: 1024
                    enabled: true
                    extensions:
                      vscode:
                        id: primary
                        extraArgs:
                          temperature: 0.2
                          nested:
                            key: value
                        extraHeaders:
                          X-Custom: static-value
            """
        )
    )
    plan = plan_vscode(
        registry,
        [{"name": "Built-in", "settings": {}}],
        owned_provider_ids=set(),
        owned_model_ids={},
        runtime=proven_runtime(),
    )
    router = next(provider for provider in plan.settings if provider["name"] == "Router")
    assert router["models"][0]["modelOptions"] == {"temperature": 0.2, "nested": {"key": "value"}}
    assert router["models"][0]["requestHeaders"] == {"X-Custom": "static-value"}


def test_current_code_projection_sanitizes_provider_key_for_secret_ids() -> None:
    model = ResolvedModel(
        provider_key="Open.Router",
        base_url="https://router.example/v1",
        api_key_reference="env.ROUTER_KEY",
        model="primary",
        display_name="Primary",
        max_output_tokens=1024,
        effective_provider="generic-chat-completion-api",
        no_image_support=False,
        favourite=False,
        factory_id="custom:primary--Open.Router",
        vscode_id="primary",
    )

    projected = project_vscode_model_snapshots((model,), proven_runtime())

    assert projected[0]["apiKey"] == "${input:chat.lm.secret.lm-open-router}"


def test_current_code_projection_rejects_colliding_secret_ids() -> None:
    models = (
        ResolvedModel(
            provider_key="Open.Router",
            base_url="https://router.example/v1",
            api_key_reference="env.ROUTER_KEY",
            model="primary",
            display_name="Primary",
            max_output_tokens=1024,
            effective_provider="generic-chat-completion-api",
            no_image_support=False,
            favourite=False,
            factory_id="custom:primary--Open.Router",
            vscode_id="primary",
        ),
        ResolvedModel(
            provider_key="open-router",
            base_url="https://router.example/v1",
            api_key_reference="env.ROUTER_KEY",
            model="secondary",
            display_name="Secondary",
            max_output_tokens=1024,
            effective_provider="generic-chat-completion-api",
            no_image_support=False,
            favourite=False,
            factory_id="custom:secondary--open-router",
            vscode_id="secondary",
        ),
    )

    with pytest.raises(AppError, match="share secret identifier"):
        project_vscode_model_snapshots(models, proven_runtime())


def test_current_code_projection_rejects_unowned_provider_name_collision() -> None:
    runtime = proven_runtime()
    model = ResolvedModel(
        provider_key="router",
        base_url="https://router.example/v1",
        api_key_reference="env.ROUTER_KEY",
        model="primary",
        display_name="Primary",
        max_output_tokens=1024,
        effective_provider="generic-chat-completion-api",
        no_image_support=False,
        favourite=False,
        factory_id="custom:primary--router",
        vscode_id="primary",
        provider_name="Router",
    )
    generated = project_vscode_model_snapshots((model,), runtime)
    existing = dict(generated[0])
    existing["vendor"] = "foreign"

    with pytest.raises(CollisionError):
        plan_vscode_models(
            (model,),
            [existing],
            owned_provider_ids=set(),
            owned_model_ids={},
            runtime=runtime,
        )


# --- Category 5: plan foreign preservation / stale owned / collision / duplicates ---


def test_plan_vscode_adopts_exact_unowned_provider_and_preserves_foreign_models() -> None:
    registry = load_registry_text(REGISTRY)
    runtime = proven_runtime()
    generated = project_vscode_providers(registry, runtime)[0]
    existing = dict(generated)
    existing["models"] = [*generated["models"], {"id": "foreign-model", "foreign": True}]

    plan = plan_vscode(
        registry,
        {"providers": [existing]},
        owned_provider_ids=set(),
        owned_model_ids={},
        runtime=runtime,
    )

    provider = plan.settings["providers"][0]
    assert provider["id"] == "ModFig/router"
    assert provider["models"][-1] == {"id": "foreign-model", "foreign": True}
    assert plan.owned_provider_ids == frozenset({"ModFig/router"})


def test_plan_vscode_rejects_divergent_unowned_namespaced_provider() -> None:
    registry = load_registry_text(REGISTRY)
    runtime = proven_runtime()
    settings = {
        "providers": [
            {"id": "ModFig/router", "baseUrl": "https://different.example/v1", "models": []},
        ],
    }

    with pytest.raises(CollisionError):
        plan_vscode(
            registry,
            settings,
            owned_provider_ids=set(),
            owned_model_ids={},
            runtime=runtime,
        )


def test_plan_vscode_preserves_foreign_providers_and_removes_stale_owned() -> None:
    registry = load_registry_text(REGISTRY)
    runtime = proven_runtime()
    settings = {
        "foreignTop": {"keep": True},
        "providers": [
            {"id": "foreign", "foreignField": True, "models": [{"id": "foreign-model"}]},
            {
                "id": "ModFig/router",
                "foreignField": True,
                "models": [
                    {"id": "foreign-model-2", "keep": True},
                    {"id": "primary", "old": True},
                    {"id": "custom:stale--router", "old": True},
                ],
            },
        ],
    }

    plan = plan_vscode(
        registry,
        settings,
        owned_provider_ids={"ModFig/router"},
        owned_model_ids={"ModFig/router": {"primary", "custom:stale--router"}},
        runtime=runtime,
    )

    assert plan.settings["foreignTop"] == {"keep": True}
    provider_ids = [p["id"] for p in plan.settings["providers"]]
    assert provider_ids == ["foreign", "ModFig/router"]
    foreign = plan.settings["providers"][0]
    assert foreign["foreignField"] is True
    assert foreign["models"] == [{"id": "foreign-model"}]
    router = plan.settings["providers"][1]
    assert router["foreignField"] is True
    assert [m["id"] for m in router["models"]] == [
        "foreign-model-2",
        "primary",
        "routed",
    ]
    assert plan.owned_model_ids == {"ModFig/router": frozenset({"primary", "routed"})}


def test_plan_vscode_retains_foreign_provider_shell_without_generated_models() -> None:
    registry = load_registry_text(REGISTRY)
    runtime = proven_runtime()
    settings = {
        "providers": [
            {"id": "foreign", "foreignField": True, "models": [{"id": "foreign-model"}]},
        ],
    }

    plan = plan_vscode(
        registry,
        settings,
        owned_provider_ids={"ModFig/router"},
        owned_model_ids={},
        runtime=runtime,
    )

    assert [p["id"] for p in plan.settings["providers"]] == ["foreign", "ModFig/router"]
    assert plan.settings["providers"][0]["models"] == [{"id": "foreign-model"}]


def test_plan_vscode_validates_ownership_before_runtime_preflight() -> None:
    registry = load_registry_text(REGISTRY)
    runtime = VSCodeRuntime(
        supported_os=("macos",),
        supported_channels=("stable",),
        supported_profile_modes=("default",),
        user_data_root=Path.cwd() / ".modfig-test" / "User",
        settings_path=Path.cwd() / ".modfig-test" / "User" / "chatLanguageModels.json",
        state_db_path=Path.cwd() / ".modfig-test" / "state.vscdb",
        state_wal_path=Path.cwd() / ".modfig-test" / "state.vscdb-wal",
        state_shm_path=Path.cwd() / ".modfig-test" / "state.vscdb-shm",
        safe_storage_supported=False,
        key_context="",
        process_quiescent=False,
        vendor_api_type_mapping=False,
    )

    with pytest.raises(AppError, match="ownership|ModFig"):
        plan_vscode(
            registry,
            {"providers": []},
            owned_provider_ids={"foreign"},
            owned_model_ids={},
            runtime=runtime,
        )


@pytest.mark.parametrize("owned_provider_ids", [{"foreign"}, {1}])
def test_plan_vscode_rejects_unnamespaced_or_nonstring_owned_provider_ids(
    owned_provider_ids: set[object],
) -> None:
    registry = load_registry_text(REGISTRY)

    with pytest.raises(AppError, match="ownership|ModFig"):
        plan_vscode(
            registry,
            {"providers": [{"id": "foreign", "models": []}]},
            owned_provider_ids=owned_provider_ids,  # type: ignore[arg-type]
            owned_model_ids={},
            runtime=proven_runtime(),
        )


@pytest.mark.parametrize(
    ("owned_provider_ids", "owned_model_ids"),
    [
        (set(), {"ModFig/router": {"primary"}}),
        ({"ModFig/router"}, {"foreign": {"primary"}}),
    ],
)
def test_plan_vscode_rejects_model_ownership_for_unowned_or_unnamespaced_provider(
    owned_provider_ids: set[str], owned_model_ids: dict[str, set[str]]
) -> None:
    registry = load_registry_text(REGISTRY)

    with pytest.raises(AppError, match="ownership|ModFig"):
        plan_vscode(
            registry,
            {"providers": [{"id": "foreign", "models": []}]},
            owned_provider_ids=owned_provider_ids,
            owned_model_ids=owned_model_ids,
            runtime=proven_runtime(),
        )


def test_plan_vscode_rejects_duplicate_existing_provider_ids() -> None:
    registry = load_registry_text(REGISTRY)
    runtime = proven_runtime()
    settings = {"providers": [{"id": "foreign", "models": []}, {"id": "foreign", "models": []}]}

    with pytest.raises(AppError, match="duplicate.*foreign"):
        plan_vscode(
            registry,
            settings,
            owned_provider_ids=set(),
            owned_model_ids={},
            runtime=runtime,
        )


def test_plan_vscode_rejects_duplicate_existing_model_ids_within_provider() -> None:
    registry = load_registry_text(REGISTRY)
    runtime = proven_runtime()
    settings = {
        "providers": [
            {"id": "foreign", "models": [{"id": "duplicate"}, {"id": "duplicate"}]},
        ],
    }

    with pytest.raises(AppError, match="duplicate.*duplicate"):
        plan_vscode(
            registry,
            settings,
            owned_provider_ids=set(),
            owned_model_ids={},
            runtime=runtime,
        )


def test_plan_vscode_rejects_divergent_unowned_model_collision() -> None:
    registry = load_registry_text(REGISTRY)
    runtime = proven_runtime()
    settings = {
        "providers": [
            {"id": "ModFig/router", "models": [{"id": "primary", "model": "different"}]},
        ],
    }

    with pytest.raises(CollisionError):
        plan_vscode(
            registry,
            settings,
            owned_provider_ids=set(),
            owned_model_ids={},
            runtime=runtime,
        )


def test_plan_vscode_preserves_foreign_models_in_stale_owned_provider() -> None:
    registry = load_registry_text(REGISTRY)
    runtime = proven_runtime()
    settings = {
        "providers": [
            {
                "id": "ModFig/stale-router",
                "foreignField": True,
                "models": [
                    {"id": "router-only", "owned": True},
                    {"id": "primary", "foreign": True},
                ],
            },
        ],
    }

    plan = plan_vscode(
        registry,
        settings,
        owned_provider_ids={"ModFig/stale-router", "ModFig/router"},
        owned_model_ids={
            "ModFig/stale-router": {"router-only"},
            "ModFig/router": {"primary"},
        },
        runtime=runtime,
    )

    stale = plan.settings["providers"][0]
    assert stale["id"] == "ModFig/stale-router"
    assert stale["foreignField"] is True
    assert stale["models"] == [{"id": "primary", "foreign": True}]


def test_plan_vscode_removes_stale_owned_provider_without_foreign_state() -> None:
    registry = load_registry_text(REGISTRY)
    runtime = proven_runtime()
    settings = {
        "providers": [
            {
                "id": "ModFig/stale-router",
                "baseUrl": "https://old.example/v1",
                "apiKey": "env.OLD_KEY",
                "models": [],
            },
            {"id": "foreign", "models": [{"id": "foreign-model"}]},
        ],
    }

    plan = plan_vscode(
        registry,
        settings,
        owned_provider_ids={"ModFig/stale-router", "ModFig/router"},
        owned_model_ids={"ModFig/router": {"primary", "routed"}},
        runtime=runtime,
    )

    provider_ids = [p["id"] for p in plan.settings["providers"]]
    assert "ModFig/stale-router" not in provider_ids
    assert provider_ids == ["foreign", "ModFig/router"]


def test_plan_vscode_preserves_provider_order_and_unknown_fields() -> None:
    registry = load_registry_text(REGISTRY)
    runtime = proven_runtime()
    settings = {
        "unknownTop": "keep",
        "providers": [
            {"id": "first-foreign", "unknown": True, "models": []},
            {"id": "ModFig/router", "models": [{"id": "primary"}]},
            {"id": "middle-foreign", "models": [{"id": "foreign-model"}]},
        ],
    }

    plan = plan_vscode(
        registry,
        settings,
        owned_provider_ids={"ModFig/router"},
        owned_model_ids={"ModFig/router": {"primary"}},
        runtime=runtime,
    )

    assert plan.settings["unknownTop"] == "keep"
    assert [p["id"] for p in plan.settings["providers"]] == [
        "first-foreign",
        "ModFig/router",
        "middle-foreign",
    ]
    assert plan.settings["providers"][0]["unknown"] is True
