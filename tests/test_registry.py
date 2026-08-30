from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from modfig.components import ExtensionComponent
from modfig.errors import AppError
from modfig.registry import (
    FactoryNativeReference,
    ModelReference,
    RegistryValidationError,
    load_registry,
    load_registry_text,
)

POSIX_SECURE_IO = pytest.mark.skipif(os.name == "nt", reason="requires native POSIX secure I/O")

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "spec" / "fixtures"

V01_FACTORY_AND_CURSOR = textwrap.dedent(
    """\
    specVersion: "0.1"
    providers:
      router:
        name: Router
        targets: [factory, cursor]
        baseUrl: https://router.example/v1
        apiKey: env.ROUTER_KEY
        provider: openai
        enabled: true
        models:
          primary:
            displayName: Primary
            contextWindow: 8192
            maxOutputTokens: 1024
            enabled: true
    clientConfig:
      factory:
        core:
          defaults:
            worker: {provider: router, model: primary}
            thinker: {provider: router, model: primary}
            orchestrator: {provider: router, model: primary}
            simple: {provider: router, model: primary}
            validator: {provider: router, model: primary}
          session:
            model: {provider: router, model: primary}
            reasoningEffort: high
            specModeModel: {factoryNative: claude-opus-4-8}
            specModeReasoningEffort: max
          mission:
            orchestratorModel: {provider: router, model: primary}
            orchestratorReasoningEffort: max
            workerModel: {provider: router, model: primary}
            workerReasoningEffort: high
            validationWorkerModel: {factoryNative: native-validator}
            validationWorkerReasoningEffort: low
        extensions:
          oh-my-droid:
            droids:
              analyst: {provider: router, model: primary}
            prune: false
      cursor:
        core:
          profile: work
    """
)


def fixture_text(kind: str, name: str) -> str:
    return (FIXTURE_DIR / kind / name).read_text(encoding="utf-8")


def registry_text(provider: str | None = "openai", model_provider: str | None = None) -> str:
    provider_line = f"            provider: {provider}\n" if provider is not None else ""
    model_provider_line = (
        f"                provider: {model_provider}\n" if model_provider is not None else ""
    )
    return textwrap.dedent(
        f"""\
        specVersion: "0.1"
        providers:
          router:
            name: Router
            targets: [factory]
            baseUrl: https://router.example/v1
            apiKey: env.ROUTER_KEY
{provider_line}            enabled: true
            models:
              chat-model:
                displayName: Chat Model
                contextWindow: 8192
                maxOutputTokens: 1024
{model_provider_line}                enabled: true
        """
    )


def _probe_registry(
    *,
    provider_key: str = "probe",
    name: str = "Probe",
    targets: str = "[factory]",
    base_url: str = "https://example.com/v1",
    api_key: str = "env.API_KEY",
    enabled: str = "false",
    provider_protocol: str | None = None,
    model_key: str = "model",
    display_name: str = "Model",
    provider_ext: str = "",
    model_ext: str = "",
) -> str:
    """Build a single-provider, single-model map-shaped registry for extension/field probes."""
    protocol_line = f"    provider: {provider_protocol}\n" if provider_protocol else ""
    return (
        'specVersion: "0.1"\n'
        "providers:\n"
        f"  {provider_key}:\n"
        f"    name: {name}\n"
        f"    targets: {targets}\n"
        f"    baseUrl: {base_url}\n"
        f"    apiKey: {api_key}\n"
        + protocol_line
        + f"    enabled: {enabled}\n"
        + provider_ext
        + "    models:\n"
        f"      {model_key}:\n"
        f"        displayName: {display_name}\n"
        "        contextWindow: 8192\n"
        "        maxOutputTokens: 1024\n" + f"        enabled: {enabled}\n" + model_ext
    )


def test_map_providers_preserve_insertion_order() -> None:
    content = textwrap.dedent(
        """\
        specVersion: "0.1"
        providers:
          zeta:
            name: Zeta
            targets: [factory]
            baseUrl: https://zeta.example/v1
            apiKey: env.ZETA_KEY
            enabled: true
            models:
              z1:
                displayName: Z1
                contextWindow: 8192
                maxOutputTokens: 1024
                enabled: true
          alpha:
            name: Alpha
            targets: [factory]
            baseUrl: https://alpha.example/v1
            apiKey: env.ALPHA_KEY
            enabled: true
            models:
              a1:
                displayName: A1
                contextWindow: 8192
                maxOutputTokens: 1024
                enabled: true
        """
    )
    registry = load_registry_text(content)
    assert [provider.key for provider in registry.providers] == ["zeta", "alpha"]
    assert registry.providers[0].models[0].model == "z1"
    assert registry.providers[1].models[0].model == "a1"


def test_map_models_preserve_insertion_order_and_key_identity() -> None:
    content = textwrap.dedent(
        """\
        specVersion: "0.1"
        providers:
          router:
            name: Router
            targets: [factory]
            baseUrl: https://router.example/v1
            apiKey: env.ROUTER_KEY
            enabled: true
            models:
              gamma:
                displayName: Gamma
                contextWindow: 8192
                maxOutputTokens: 1024
                enabled: true
              alpha:
                displayName: Alpha
                contextWindow: 8192
                maxOutputTokens: 1024
                enabled: true
              beta:
                displayName: Beta
                contextWindow: 8192
                maxOutputTokens: 1024
                enabled: true
        """
    )
    registry = load_registry_text(content)
    assert [model.model for model in registry.providers[0].models] == ["gamma", "alpha", "beta"]
    provider, model = registry.resolve_model(ModelReference("router", "beta"), "factory")
    assert provider.key == "router"
    assert model.model == "beta"


def test_map_providers_list_shape_rejected() -> None:
    content = textwrap.dedent(
        """\
        specVersion: "0.1"
        providers:
          - key: router
            name: Router
            targets: [factory]
            baseUrl: https://router.example/v1
            apiKey: env.ROUTER_KEY
            enabled: true
            models:
              - model: primary
                displayName: Primary
                contextWindow: 8192
                maxOutputTokens: 1024
                enabled: true
        """
    )
    with pytest.raises(RegistryValidationError, match="providers must be a non-empty mapping"):
        load_registry_text(content)


def test_map_models_list_shape_rejected() -> None:
    content = textwrap.dedent(
        """\
        specVersion: "0.1"
        providers:
          router:
            name: Router
            targets: [factory]
            baseUrl: https://router.example/v1
            apiKey: env.ROUTER_KEY
            enabled: true
            models:
              - model: primary
                displayName: Primary
                contextWindow: 8192
                maxOutputTokens: 1024
                enabled: true
        """
    )
    with pytest.raises(RegistryValidationError, match="models must be a non-empty mapping"):
        load_registry_text(content)


def test_map_provider_inline_key_rejected() -> None:
    content = registry_text().replace("  router:\n", "  router:\n    key: router\n", 1)
    with pytest.raises(RegistryValidationError, match="unknown field.*'key'"):
        load_registry_text(content)


def test_map_model_inline_field_rejected() -> None:
    content = registry_text().replace(
        "      chat-model:\n", "      chat-model:\n        model: chat-model\n", 1
    )
    with pytest.raises(RegistryValidationError, match="unknown field.*'model'"):
        load_registry_text(content)


def test_map_duplicate_provider_key_rejected() -> None:
    content = registry_text().replace("  router:\n", "  router:\n  router:\n", 1)
    with pytest.raises(RegistryValidationError, match="duplicate key"):
        load_registry_text(content)


def test_map_empty_provider_key_rejected() -> None:
    content = registry_text().replace("  router:", '  "":', 1)
    with pytest.raises(RegistryValidationError, match="providers key.*non-empty string"):
        load_registry_text(content)


def test_map_non_string_provider_key_rejected() -> None:
    content = registry_text().replace("  router:", "  123:", 1)
    with pytest.raises(RegistryValidationError, match="providers key.*non-empty string"):
        load_registry_text(content)


def test_map_empty_model_key_rejected() -> None:
    content = registry_text().replace("      chat-model:", '      "":', 1)
    with pytest.raises(RegistryValidationError, match="models key.*non-empty string"):
        load_registry_text(content)


def test_v01_resolver_rejects_unavailable_models() -> None:
    cases = [
        (ModelReference("missing", "primary"), "factory", "unknown provider"),
        (ModelReference("router", "missing"), "factory", "unknown model"),
        (ModelReference("router", "primary"), "vscode", "does not target"),
    ]
    registry = load_registry_text(V01_FACTORY_AND_CURSOR)

    for reference, client, message in cases:
        with pytest.raises(RegistryValidationError, match=message):
            registry.resolve_model(reference, client)

    disabled_provider = load_registry_text(
        V01_FACTORY_AND_CURSOR.replace("enabled: true", "enabled: false", 1)
    )
    with pytest.raises(RegistryValidationError, match="disabled provider"):
        disabled_provider.resolve_model(ModelReference("router", "primary"), "factory")

    marker = "maxOutputTokens: 1024\n        enabled: true"
    disabled_model = load_registry_text(
        V01_FACTORY_AND_CURSOR.replace(marker, marker.replace("true", "false"))
    )
    with pytest.raises(RegistryValidationError, match="disabled model"):
        disabled_model.resolve_model(ModelReference("router", "primary"), "factory")


@pytest.mark.parametrize("effort", ["", "extreme", 1, True])
def test_v01_rejects_invalid_factory_reasoning_effort(effort: object) -> None:
    rendered = (
        '""' if effort == "" else str(effort).lower() if isinstance(effort, bool) else str(effort)
    )
    invalid = V01_FACTORY_AND_CURSOR.replace(
        "reasoningEffort: high", f"reasoningEffort: {rendered}", 1
    )

    with pytest.raises(RegistryValidationError, match="reasoningEffort"):
        load_registry_text(invalid)


@pytest.mark.parametrize(
    "old,new",
    [
        ("worker: {provider: router, model: primary}", "worker: {factoryNative: native}"),
        ("reasoningEffort: high", "reasoningEffort: {factoryNative: native}"),
        ("provider: router, model: primary", "factoryNative: native, extra: bad"),
    ],
)
def test_v01_rejects_factory_native_outside_allowed_model_positions(old: str, new: str) -> None:
    with pytest.raises(
        RegistryValidationError, match="factoryNative|reasoningEffort|provider and model"
    ):
        load_registry_text(V01_FACTORY_AND_CURSOR.replace(old, new, 1))


def test_model_provider_overrides_provider_protocol() -> None:
    registry = load_registry_text(registry_text("generic-chat-completion-api", "OpenRouter"))

    model = registry.providers[0].models[0]

    assert model.effective_provider == "OpenRouter"


def test_provider_protocol_is_used_when_model_override_is_absent() -> None:
    registry = load_registry_text(registry_text("anthropic"))

    assert registry.providers[0].models[0].effective_provider == "anthropic"


def test_effective_provider_defaults_to_generic_chat_completion_api() -> None:
    registry = load_registry_text(registry_text(None))

    assert registry.providers[0].models[0].effective_provider == "generic-chat-completion-api"


def test_chatgpt_effective_identities_use_explicit_values_or_defaults() -> None:
    chatgpt_registry = registry_text("openai").replace("targets: [factory]", "targets: [chatgpt]")
    chatgpt_registry = chatgpt_registry.replace(
        "    enabled: true\n    models:",
        "    enabled: true\n    extensions:\n      chatgpt:\n        default: true\n    models:",
        1,
    )
    registry = load_registry_text(chatgpt_registry)
    provider = registry.providers[0]

    assert provider.chatgpt_provider_id() == "modfig-router"
    assert provider.models[0].chatgpt_catalog_id() == "chat-model"

    explicit = load_registry_text(
        textwrap.dedent(
            """\
            specVersion: "0.1"
            providers:
              router:
                name: Router
                targets: [chatgpt]
                baseUrl: https://router.example/v1
                apiKey: env.ROUTER_KEY
                enabled: true
                extensions:
                  chatgpt:
                    providerId: modfig-explicit
                    wireApi: responses
                    default: true
                models:
                  chat-model:
                    displayName: Chat Model
                    contextWindow: 8192
                    maxOutputTokens: 1024
                    enabled: true
                    extensions:
                      chatgpt:
                        catalogId: explicit-model
            """
        )
    )

    assert explicit.providers[0].chatgpt_provider_id() == "modfig-explicit"
    assert explicit.providers[0].models[0].chatgpt_catalog_id() == "explicit-model"


def test_chatgpt_generic_provider_can_opt_into_responses_transport() -> None:
    registry = load_registry_text(
        textwrap.dedent(
            """\
            specVersion: "0.1"
            providers:
              surplus:
                name: Surplus
                targets: [factory, chatgpt]
                baseUrl: https://api.surplusintelligence.ai/v1
                apiKey: env.SURPLUS_API_KEY
                provider: generic-chat-completion-api
                enabled: true
                extensions:
                  chatgpt:
                    wireApi: responses
                    default: true
                models:
                  gpt-5.6-sol:
                    displayName: GPT-5.6 Sol
                    contextWindow: 1048576
                    maxOutputTokens: 128000
                    enabled: true
            """
        )
    )

    provider = registry.providers[0]
    assert provider.models[0].effective_provider == "generic-chat-completion-api"
    assert provider.chatgpt_wire_api() == "responses"


def test_chatgpt_reasoning_levels_parse_on_model_extensions() -> None:
    registry = load_registry_text(
        _probe_registry(
            targets="[chatgpt]",
            enabled="true",
            provider_protocol="openai",
            provider_ext="    extensions:\n      chatgpt:\n        default: true\n",
            model_ext=(
                "        extensions:\n"
                "          chatgpt:\n"
                "            reasoningLevels: [low, medium, high, xhigh, max]\n"
            ),
        )
    )

    assert registry.providers[0].models[0].chatgpt_reasoning_levels == (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )


def test_vscode_reasoning_levels_parse_on_model_extensions() -> None:
    registry = load_registry_text(
        _probe_registry(
            targets="[vscode]",
            enabled="true",
            model_ext=(
                "        extensions:\n"
                "          vscode:\n"
                "            reasoningLevels: [low, medium, high, xhigh, max]\n"
                "            defaultReasoningLevel: max\n"
            ),
        )
    )

    model = registry.providers[0].models[0]
    assert model.vscode_reasoning_levels == ("low", "medium", "high", "xhigh", "max")
    assert model.vscode_default_reasoning_level == "max"


@pytest.mark.parametrize(
    "extension",
    [
        "reasoningLevels: []",
        "reasoningLevels: [medium, medium]",
        "reasoningLevels: [low, ultra]",
        "reasoningLevels: [low]\n            defaultReasoningLevel: max",
    ],
)
def test_vscode_reasoning_levels_reject_invalid_values(extension: str) -> None:
    with pytest.raises(RegistryValidationError, match="reasoningLevels|defaultReasoningLevel"):
        load_registry_text(
            _probe_registry(
                targets="[vscode]",
                enabled="true",
                model_ext=(f"        extensions:\n          vscode:\n            {extension}\n"),
            )
        )


@pytest.mark.parametrize(
    "levels",
    ["[]", "[medium, medium]", "[low, extreme]", "not-a-list"],
)
def test_chatgpt_reasoning_levels_reject_invalid_values(levels: str) -> None:
    with pytest.raises(RegistryValidationError, match="reasoningLevels"):
        load_registry_text(
            _probe_registry(
                targets="[chatgpt]",
                enabled="true",
                provider_protocol="openai",
                provider_ext="    extensions:\n      chatgpt:\n        default: true\n",
                model_ext=(
                    "        extensions:\n"
                    "          chatgpt:\n"
                    f"            reasoningLevels: {levels}\n"
                ),
            )
        )


def test_extension_component_rejects_reserved_or_invalid_names() -> None:
    for name in ("core", "Bad_Name", "-bad"):
        with pytest.raises(ValueError, match="extension"):
            ExtensionComponent(name)


def test_v01_preserves_logical_client_names_without_package_ids() -> None:
    registry = load_registry_text(V01_FACTORY_AND_CURSOR)

    assert set(registry.client_config) == {"factory", "cursor"}
    extension = registry.client_component("factory", ExtensionComponent("oh-my-droid"))
    assert extension is not None
    assert extension["droids"]["analyst"] == ModelReference("router", "primary")
    assert extension["prune"] is False


def test_v01_parses_typed_factory_model_selections() -> None:
    registry = load_registry_text(V01_FACTORY_AND_CURSOR)
    core = registry.client_component("factory", "core")

    assert core is not None
    assert isinstance(core["defaults"]["worker"], ModelReference)
    assert isinstance(core["session"]["specModeModel"], FactoryNativeReference)


def test_v01_parses_factory_subagent_model_selections() -> None:
    text = V01_FACTORY_AND_CURSOR.replace(
        "        validationWorkerReasoningEffort: low\n",
        "        validationWorkerReasoningEffort: low\n"
        "      subagent:\n"
        "        lightModel: {provider: router, model: primary}\n"
        "        mediumModel: {provider: router, model: primary}\n"
        "        heavyModel: {factoryNative: native-heavy}\n",
    )
    registry = load_registry_text(text)
    core = registry.client_component("factory", "core")

    assert core is not None
    assert isinstance(core["subagent"]["lightModel"], ModelReference)
    assert isinstance(core["subagent"]["mediumModel"], ModelReference)
    assert isinstance(core["subagent"]["heavyModel"], FactoryNativeReference)


def test_v01_resolves_a_portable_factory_reference() -> None:
    registry = load_registry_text(V01_FACTORY_AND_CURSOR)

    provider, model = registry.resolve_model(ModelReference("router", "primary"), "factory")

    assert provider.key == "router"
    assert model.factory_id(provider.key) == "custom:primary--router"


def test_v01_rejects_root_extensions() -> None:
    with pytest.raises(RegistryValidationError, match="unknown field.*extensions"):
        load_registry_text(registry_text() + "extensions: {}\n")


def test_v01_rejects_core_as_extension_name() -> None:
    invalid = V01_FACTORY_AND_CURSOR.replace("oh-my-droid:", "core:")

    with pytest.raises(RegistryValidationError, match="extension.*core.*reserved"):
        load_registry_text(invalid)


def test_v01_accepts_dynamic_logical_provider_target() -> None:
    registry = load_registry_text(registry_text().replace("[factory]", "[cursor]"))

    assert registry.providers[0].targets == ("cursor",)


@pytest.mark.parametrize("target", ["ChatGPT", "Factory", "VSCode", "bad_name", "-bad"])
def test_target_casing_is_rejected(target: str) -> None:
    with pytest.raises(RegistryValidationError, match="logical client"):
        load_registry_text(registry_text().replace("targets: [factory]", f"targets: [{target}]"))


@pytest.mark.parametrize(
    ("level", "name"),
    [
        ("provider", "chatgpt"),
        ("model", "vscode"),
        ("model", "chatgpt"),
    ],
)
def test_nested_target_extension_mappings_reject_null(level: str, name: str) -> None:
    if level == "provider":
        provider_ext = f"    extensions:\n      {name}: null\n"
        model_ext = ""
    else:
        provider_ext = ""
        model_ext = f"        extensions:\n          {name}: null\n"
    with pytest.raises(RegistryValidationError, match="must be a mapping"):
        load_registry_text(_probe_registry(provider_ext=provider_ext, model_ext=model_ext))


@pytest.mark.parametrize(
    ("level", "extension", "field", "message"),
    [
        ("provider", "chatgpt", "providerId", "providerId"),
        ("provider", "chatgpt", "wireApi", "wireApi"),
        ("model", "vscode", "id", "vscode.id"),
        ("model", "chatgpt", "catalogId", "catalogId"),
    ],
)
def test_explicit_target_extension_fields_reject_null(
    level: str, extension: str, field: str, message: str
) -> None:
    if level == "provider":
        provider_ext = f"    extensions:\n      {extension}:\n        {field}: null\n"
        model_ext = ""
    else:
        provider_ext = ""
        model_ext = f"        extensions:\n          {extension}:\n            {field}: null\n"
    with pytest.raises(RegistryValidationError, match=message):
        load_registry_text(_probe_registry(provider_ext=provider_ext, model_ext=model_ext))


def test_default_chatgpt_catalog_id_must_be_safe() -> None:
    content = _probe_registry(
        targets="[chatgpt]",
        enabled="true",
        provider_protocol="openai",
        model_key='"bad id"',
        display_name="Bad Identity",
    )
    with pytest.raises(RegistryValidationError, match="catalog id"):
        load_registry_text(content)


def test_factory_extension_rejects_unknown_fields() -> None:
    # VAL-CATALOG-004: the model-level extensions.factory namespace is a narrow
    # pass-through for {providers, extraArgs, extraHeaders}; stored Factory IDs
    # are still always computed, so declaring one must be rejected as an
    # unknown field.
    content = _probe_registry(
        provider_key="router",
        name="Router",
        targets="[factory]",
        enabled="true",
        provider_protocol="openai",
        model_ext="        extensions:\n          factory:\n            id: custom:model--router\n",
    )
    with pytest.raises(RegistryValidationError, match="contains unknown field 'id'"):
        load_registry_text(content)


def test_factory_extension_accepts_providers_and_passthroughs() -> None:
    # VAL-PIN-001: extensions.factory carries the Surplus provider-pinning
    # allow-list (providers, merged as a request-body provider array) plus
    # unvalidated extraArgs/extraHeaders passthroughs.
    content = _probe_registry(
        provider_key="surplus",
        name="Surplus",
        targets="[factory]",
        enabled="true",
        provider_protocol="generic-chat-completion-api",
        model_ext=(
            "        extensions:\n"
            "          factory:\n"
            "            providers: [openai]\n"
            "            extraArgs:\n"
            "              max_price_per_1m: 8.0\n"
            "              pinned: true\n"
            "              nested:\n"
            "                key: value\n"
            "            extraHeaders:\n"
            "              X-Pin: static\n"
        ),
    )
    registry = load_registry_text(content)
    model = registry.providers[0].models[0]
    assert model.factory_providers() == ("openai",)
    assert model.factory_extra_args() == {
        "provider": ["openai"],
        "max_price_per_1m": 8.0,
        "pinned": True,
        "nested": {"key": "value"},
    }
    assert model.factory_extra_headers() == {"X-Pin": "static"}


def test_factory_extension_accepts_passthroughs_without_providers() -> None:
    # VAL-PIN-001: extraArgs-only is valid; the provider key is only merged in
    # when the providers allow-list is present.
    content = _probe_registry(
        provider_key="surplus",
        name="Surplus",
        targets="[factory]",
        enabled="true",
        model_ext=(
            "        extensions:\n"
            "          factory:\n"
            "            extraArgs:\n"
            "              temperature: 0.2\n"
        ),
    )
    model = load_registry_text(content).providers[0].models[0]
    assert model.factory_extra_args() == {"temperature": 0.2}
    assert model.factory_providers() is None


def test_factory_extension_rejects_invalid_providers() -> None:
    def model_ext_for(providers: str) -> str:
        return f"        extensions:\n          factory:\n            providers: {providers}\n"

    load_registry_text(
        _probe_registry(
            provider_key="surplus",
            name="Surplus",
            targets="[factory]",
            enabled="true",
            # duplicates are fine; the shape is what matters
            model_ext=model_ext_for("[openai, openai]"),
        )
    )
    for bad_list in ("[]", "[openai, 5]", "[openai, '']"):
        bad_content = _probe_registry(
            provider_key="surplus",
            name="Surplus",
            targets="[factory]",
            enabled="true",
            model_ext=model_ext_for(bad_list),
        )
        with pytest.raises(RegistryValidationError, match="providers must be"):
            load_registry_text(bad_content)


def test_factory_extension_rejects_non_mapping_passthroughs() -> None:
    for passthrough_key in ("extraArgs", "extraHeaders"):
        content = _probe_registry(
            provider_key="surplus",
            name="Surplus",
            targets="[factory]",
            enabled="true",
            model_ext=(
                "        extensions:\n"
                f"          factory:\n"
                f"            {passthrough_key}: not-a-mapping\n"
            ),
        )
        with pytest.raises(RegistryValidationError, match="must be a mapping"):
            load_registry_text(content)


def test_factory_extension_accepts_opaque_extra_args_values() -> None:
    # VAL-PIN-001: extraArgs values are never type-checked; arbitrary YAML/JSON
    # shapes (even NaN) flow through to the request body.
    content = _probe_registry(
        provider_key="surplus",
        name="Surplus",
        targets="[factory]",
        enabled="true",
        model_ext=(
            "        extensions:\n"
            "          factory:\n"
            "            extraArgs:\n"
            "              price: .nan\n"
            "              anything: [1, two, {three: null}]\n"
        ),
    )
    model = load_registry_text(content).providers[0].models[0]
    extra_args = model.factory_extra_args()
    assert extra_args is not None
    assert "price" in extra_args
    assert extra_args["anything"] == [1, "two", {"three": None}]


def test_vscode_extension_accepts_passthroughs() -> None:
    content = _probe_registry(
        provider_key="surplus",
        name="Surplus",
        targets="[vscode]",
        enabled="true",
        model_ext=(
            "        extensions:\n"
            "          vscode:\n"
            "            id: custom-id\n"
            "            extraArgs:\n"
            "              temperature: 0.2\n"
            "            extraHeaders:\n"
            "              X-Pin: static\n"
        ),
    )
    model = load_registry_text(content).providers[0].models[0]
    assert model.vscode_extra_args() == {"temperature": 0.2}
    assert model.vscode_extra_headers() == {"X-Pin": "static"}


def test_vscode_extension_rejects_non_mapping_passthroughs() -> None:
    for passthrough_key in ("extraArgs", "extraHeaders"):
        content = _probe_registry(
            provider_key="surplus",
            name="Surplus",
            targets="[vscode]",
            enabled="true",
            model_ext=(
                "        extensions:\n"
                "          vscode:\n"
                f"            {passthrough_key}: not-a-mapping\n"
            ),
        )
        with pytest.raises(RegistryValidationError, match="must be a mapping"):
            load_registry_text(content)


def test_chatgpt_provider_extension_accepts_http_headers() -> None:
    content = _probe_registry(
        provider_key="surplus",
        name="Surplus",
        targets="[chatgpt]",
        enabled="true",
        provider_ext=(
            "    extensions:\n"
            "      chatgpt:\n"
            "        providerId: modfig-surplus\n"
            "        wireApi: responses\n"
            "        default: true\n"
            "        httpHeaders:\n"
            "          X-Custom: static-value\n"
        ),
    )
    provider = load_registry_text(content).providers[0]
    assert provider.chatgpt_http_headers() == {"X-Custom": "static-value"}


def test_chatgpt_provider_extension_rejects_non_string_http_headers() -> None:
    content = _probe_registry(
        provider_key="surplus",
        name="Surplus",
        targets="[chatgpt]",
        enabled="true",
        provider_ext=(
            "    extensions:\n"
            "      chatgpt:\n"
            "        providerId: modfig-surplus\n"
            "        wireApi: responses\n"
            "        default: true\n"
            "        httpHeaders:\n"
            "          X-Custom: [1, 2]\n"
        ),
    )
    with pytest.raises(RegistryValidationError, match="values must be strings"):
        load_registry_text(content)


def test_factory_id_is_computed_and_slugified() -> None:
    # VAL-CATALOG-004: Factory model IDs are always computed as
    # custom:<slugify(model)>--<provider_key>; '/' and spaces collapse to '-'.
    content = _probe_registry(
        provider_key="surplus",
        name="Surplus",
        targets="[factory]",
        enabled="true",
        model_key="OpenAI/GPT 5",
        display_name="GPT 5",
    )
    registry = load_registry_text(content)
    model = registry.providers[0].models[0]
    assert model.model == "OpenAI/GPT 5"
    assert model.factory_id("surplus") == "custom:openai-gpt-5--surplus"


def test_duplicate_computed_factory_ids_are_rejected() -> None:
    # VAL-CATALOG-005: two models whose computed Factory IDs collide fail
    # validation. "Foo Bar" and "Foo-Bar" are distinct map keys (so per-provider
    # model uniqueness passes) but both slugify to "foo-bar".
    content = _probe_registry(
        provider_key="umans-ai",
        name="Umans AI",
        targets="[factory]",
        enabled="true",
        model_ext=(
            "      Foo Bar:\n"
            "        displayName: Foo Bar\n"
            "        contextWindow: 8192\n"
            "        maxOutputTokens: 1024\n"
            "        enabled: true\n"
            "      Foo-Bar:\n"
            "        displayName: Foo Bar Variant\n"
            "        contextWindow: 8192\n"
            "        maxOutputTokens: 1024\n"
            "        enabled: true\n"
        ),
    )
    with pytest.raises(RegistryValidationError, match="duplicate effective Factory model id"):
        load_registry_text(content)


def test_chatgpt_provider_without_enabled_models_skips_emission_checks() -> None:
    content = textwrap.dedent(
        """\
        specVersion: "0.1"
        providers:
          first:
            name: First
            targets: [chatgpt]
            baseUrl: https://first.example/v1
            apiKey: env.FIRST_KEY
            provider: generic-chat-completion-api
            enabled: true
            extensions:
              chatgpt:
                providerId: modfig-shared
            models:
              first-model:
                displayName: First Model
                contextWindow: 8192
                maxOutputTokens: 1024
                enabled: false
          second:
            name: Second
            targets: [chatgpt]
            baseUrl: https://second.example/v1
            apiKey: env.SECOND_KEY
            provider: anthropic
            enabled: true
            extensions:
              chatgpt:
                providerId: modfig-shared
            models:
              second-model:
                displayName: Second Model
                contextWindow: 8192
                maxOutputTokens: 1024
                enabled: false
        """
    )
    registry = load_registry_text(content)

    assert registry.emitted_models("chatgpt") == ()


@pytest.mark.parametrize("model_name", ["gpt-5", "gpt-4o", "o3", "codex-mini"])
def test_factory_gpt_model_accepts_declared_generic_transport(model_name: str) -> None:
    # VAL-CATALOG-006: GPT-family models under a generic provider validate;
    # no static name-based transport rule rejects the declaration.
    content = registry_text("generic-chat-completion-api").replace("chat-model", model_name)
    registry = load_registry_text(content)
    assert registry.providers[0].models[0].effective_provider == "generic-chat-completion-api"


@pytest.mark.parametrize("model_name", ["gpt-5", "gpt-4o", "o3", "codex-mini"])
def test_factory_gpt_model_accepts_declared_openai_transport(model_name: str) -> None:
    # VAL-CATALOG-006: the same GPT-family model under an openai provider validates.
    content = registry_text("openai").replace("chat-model", model_name)
    registry = load_registry_text(content)
    assert registry.providers[0].models[0].effective_provider == "openai"


@pytest.mark.parametrize("provider", ["OpenRouter", "invalid-protocol"])
def test_provider_level_rejects_unknown_protocol(provider: str) -> None:
    with pytest.raises(RegistryValidationError, match="provider-level provider"):
        load_registry_text(registry_text(provider))


@pytest.mark.parametrize("value", ['""', "null"])
def test_empty_provider_values_are_rejected_but_yaml_null_is_absent(value: str) -> None:
    if value == "null":
        registry = load_registry_text(registry_text(value))
        assert registry.providers[0].models[0].effective_provider == "generic-chat-completion-api"
    else:
        with pytest.raises(RegistryValidationError, match="provider must not be empty"):
            load_registry_text(registry_text(value))


def test_duplicate_yaml_keys_are_rejected() -> None:
    content = registry_text().replace(
        "    name: Router\n", "    name: Router\n    name: Duplicate\n"
    )

    with pytest.raises(RegistryValidationError, match="duplicate key"):
        load_registry_text(content)


@POSIX_SECURE_IO
def test_load_registry_rejects_insecure_file_permissions(tmp_path: Path) -> None:
    path = tmp_path / "modfig.yaml"
    path.write_text(registry_text(), encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(AppError, match="owner-only"):
        load_registry(path)


@POSIX_SECURE_IO
def test_load_registry_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.yaml"
    target.write_text(registry_text(), encoding="utf-8")
    target.chmod(0o600)
    path = tmp_path / "modfig.yaml"
    path.symlink_to(target)

    with pytest.raises(AppError, match="must not be a symlink"):
        load_registry(path)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://router.example:abc/v1",
        "https://router.example:-1/v1",
        "https://router.example:99999/v1",
        "https://router.example:65536/v1",
        "https://user:pass@router.example/v1",
        "https://router.example/v1#frag",
    ],
)
def test_registry_rejects_invalid_or_unsafe_base_urls(base_url: str) -> None:
    content = registry_text().replace("https://router.example/v1", base_url)

    with pytest.raises(RegistryValidationError, match="baseUrl"):
        load_registry_text(content)
