from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeAlias
from urllib.parse import urlparse

import yaml

from .components import Component
from .storage import read_private_text

SUPPORTED_SPEC_VERSIONS: Final = frozenset({"0.1"})
LOGICAL_ID_RE: Final = re.compile(r"^[a-z][a-z0-9-]*$")
REASONING_EFFORTS: Final = frozenset({"off", "none", "low", "medium", "high", "max"})
PROVIDER_PROTOCOLS: Final = frozenset({"openai", "anthropic", "generic-chat-completion-api"})
API_KEY_REFERENCE_RE: Final = re.compile(r"^env\.[A-Za-z_][A-Za-z0-9_]*$")
PROVIDER_KEY_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CHATGPT_PROVIDER_ID_RE: Final = re.compile(r"^modfig-[A-Za-z0-9][A-Za-z0-9._-]*$")
CHATGPT_CATALOG_ID_RE: Final = re.compile(r"^[^\s\x00-\x1f\x7f]+$")
CHATGPT_PROFILE_KEY_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
CHATGPT_REASONING_EFFORTS: Final = (
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)
CHATGPT_REASONING_EFFORT_SET: Final = frozenset(CHATGPT_REASONING_EFFORTS)
VSCODE_REASONING_EFFORTS: Final = (
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
VSCODE_REASONING_EFFORT_SET: Final = frozenset(VSCODE_REASONING_EFFORTS)


class RegistryValidationError(ValueError):
    """Raised when a ModFig registry violates the v0.1 contract."""

    def __init__(self, issues: Sequence[str] | str) -> None:
        self.issues = [issues] if isinstance(issues, str) else list(issues)
        super().__init__("\n".join(self.issues))


class DuplicateKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that fails instead of silently overwriting duplicate keys."""


def _construct_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise RegistryValidationError(f"duplicate key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


DuplicateKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


@dataclass(frozen=True)
class ModelReference:
    provider_key: str
    model_name: str


@dataclass(frozen=True)
class FactoryNativeReference:
    identifier: str


ModelSelection: TypeAlias = ModelReference | FactoryNativeReference


@dataclass(frozen=True)
class ClientConfig:
    core: Mapping[str, object] | None
    extensions: Mapping[str, Mapping[str, object]]


@dataclass(frozen=True)
class Model:
    model: str
    display_name: str
    context_window: int
    max_output_tokens: int
    max_input_tokens: int
    enabled: bool
    provider_override: str | None
    effective_provider: str
    no_image_support: bool
    tool_calling: bool
    favourite: bool
    extensions: Mapping[str, Any] = field(default_factory=dict)
    chatgpt_reasoning_levels: tuple[str, ...] = ()
    vscode_reasoning_levels: tuple[str, ...] = ()
    vscode_default_reasoning_level: str | None = None

    def factory_id(self, provider_key: str) -> str:
        # ponytail: IDs are always derived from the model/provider keys;
        # extensions.factory carries per-model Factory settings, not IDs.
        return f"custom:{slugify(self.model)}--{provider_key}"

    def factory_extra_args(self) -> Mapping[str, Any] | None:
        """Request-body extraArgs from extensions.factory, with the providers
        allow-list merged in as ``provider`` (Surplus provider pinning)."""
        factory_extension = self.extensions.get("factory")
        if not isinstance(factory_extension, Mapping):
            return None
        merged: dict[str, Any] = {}
        raw_args = factory_extension.get("extraArgs")
        if isinstance(raw_args, Mapping):
            merged.update(raw_args)
        providers = factory_extension.get("providers")
        if isinstance(providers, (list, tuple)) and all(isinstance(p, str) for p in providers):
            merged["provider"] = list(providers)
        return merged if merged else None

    def factory_extra_headers(self) -> Mapping[str, Any] | None:
        factory_extension = self.extensions.get("factory")
        if isinstance(factory_extension, Mapping) and "extraHeaders" in factory_extension:
            extra_headers = factory_extension["extraHeaders"]
            return extra_headers if isinstance(extra_headers, Mapping) else None
        return None

    def factory_providers(self) -> tuple[str, ...] | None:
        factory_extension = self.extensions.get("factory")
        if isinstance(factory_extension, Mapping) and "providers" in factory_extension:
            providers = factory_extension["providers"]
            if isinstance(providers, (list, tuple)) and all(isinstance(p, str) for p in providers):
                return tuple(providers)
        return None

    def vscode_extra_args(self) -> Mapping[str, Any] | None:
        vscode_extension = self.extensions.get("vscode")
        if isinstance(vscode_extension, Mapping) and "extraArgs" in vscode_extension:
            extra_args = vscode_extension["extraArgs"]
            return extra_args if isinstance(extra_args, Mapping) else None
        return None

    def vscode_extra_headers(self) -> Mapping[str, Any] | None:
        vscode_extension = self.extensions.get("vscode")
        if isinstance(vscode_extension, Mapping) and "extraHeaders" in vscode_extension:
            extra_headers = vscode_extension["extraHeaders"]
            return extra_headers if isinstance(extra_headers, Mapping) else None
        return None

    def vscode_id(self) -> str:
        vscode_extension = self.extensions.get("vscode")
        if isinstance(vscode_extension, Mapping) and "id" in vscode_extension:
            return str(vscode_extension["id"])
        return self.model

    def chatgpt_catalog_id(self) -> str:
        chatgpt_extension = self.extensions.get("chatgpt")
        if isinstance(chatgpt_extension, Mapping) and "catalogId" in chatgpt_extension:
            return str(chatgpt_extension["catalogId"])
        return self.model


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    targets: tuple[str, ...]
    base_url: str
    api_key_reference: str
    enabled: bool
    provider_protocol: str | None
    models: tuple[Model, ...]
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def emits_to(self, target: str) -> bool:
        return self.enabled and target in self.targets

    def chatgpt_provider_id(self) -> str:
        chatgpt_extension = self.extensions.get("chatgpt")
        if isinstance(chatgpt_extension, Mapping) and "providerId" in chatgpt_extension:
            return str(chatgpt_extension["providerId"])
        return f"modfig-{self.key}"

    def chatgpt_wire_api(self) -> str | None:
        if self.provider_protocol == "openai":
            return "responses"
        chatgpt_extension = self.extensions.get("chatgpt")
        if isinstance(chatgpt_extension, Mapping) and "wireApi" in chatgpt_extension:
            return str(chatgpt_extension["wireApi"])
        return None

    def chatgpt_default(self) -> bool:
        chatgpt_extension = self.extensions.get("chatgpt")
        return isinstance(chatgpt_extension, Mapping) and chatgpt_extension.get("default") is True

    def chatgpt_http_headers(self) -> Mapping[str, Any] | None:
        """Static request headers for the codex provider table (http_headers)."""
        chatgpt_extension = self.extensions.get("chatgpt")
        if isinstance(chatgpt_extension, Mapping) and "httpHeaders" in chatgpt_extension:
            http_headers = chatgpt_extension["httpHeaders"]
            return http_headers if isinstance(http_headers, Mapping) else None
        return None


@dataclass(frozen=True)
class Registry:
    spec_version: str
    providers: tuple[Provider, ...]
    client_config: Mapping[str, ClientConfig] = field(default_factory=dict)

    def emitted_models(self, target: str) -> tuple[tuple[Provider, Model], ...]:
        return tuple(
            (provider, model)
            for provider in self.providers
            if provider.emits_to(target)
            for model in provider.models
            if model.enabled
        )

    def resolve_model(
        self, reference: ModelReference, logical_client: str
    ) -> tuple[Provider, Model]:
        provider = next(
            (candidate for candidate in self.providers if candidate.key == reference.provider_key),
            None,
        )
        if provider is None:
            raise RegistryValidationError(f"unknown provider {reference.provider_key!r}")
        if not provider.enabled:
            raise RegistryValidationError(f"disabled provider {reference.provider_key!r}")
        model = next(
            (candidate for candidate in provider.models if candidate.model == reference.model_name),
            None,
        )
        if model is None:
            raise RegistryValidationError(
                f"unknown model {reference.model_name!r} for provider {reference.provider_key!r}"
            )
        if not model.enabled:
            raise RegistryValidationError(
                f"disabled model {reference.model_name!r} for provider {reference.provider_key!r}"
            )
        if logical_client not in provider.targets:
            raise RegistryValidationError(
                f"provider {reference.provider_key!r} does not target {logical_client!r}"
            )
        return provider, model

    def client_component(
        self, logical_client: str, component: Component
    ) -> Mapping[str, object] | None:
        config = self.client_config.get(logical_client)
        if config is None:
            return None
        if component == "core":
            return config.core
        return config.extensions.get(component.name)


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower())
    slug = slug.strip(".-")
    if not slug:
        raise RegistryValidationError("model produces an empty Factory slug")
    return slug


def load_registry(path: Path) -> Registry:
    return load_registry_text(read_private_text(path, "registry"))


def load_registry_text(text: str) -> Registry:
    if not text.strip():
        raise RegistryValidationError("registry is empty")
    try:
        raw = yaml.load(text, Loader=DuplicateKeySafeLoader)
    except RegistryValidationError:
        raise
    except yaml.YAMLError as exc:
        raise RegistryValidationError(f"invalid YAML: {exc}") from exc
    return _parse_registry(raw)


def _parse_registry(raw: Any) -> Registry:
    issues: list[str] = []
    root = _mapping(raw, "registry", issues)
    _reject_unknown_fields(root, {"specVersion", "providers", "clientConfig"}, "registry", issues)

    spec_version = _required_string(root, "specVersion", "registry", issues)
    if spec_version and spec_version not in SUPPORTED_SPEC_VERSIONS:
        supported_versions = ", ".join(sorted(SUPPORTED_SPEC_VERSIONS))
        issues.append(f"unsupported specVersion {spec_version!r}; supported: {supported_versions}")

    client_config = _parse_client_config(root.get("clientConfig", {}), issues)
    providers_raw = root.get("providers")
    if not isinstance(providers_raw, Mapping) or not providers_raw:
        issues.append("registry.providers must be a non-empty mapping")
        providers_raw = {}

    providers = tuple(
        _parse_provider(key, value, issues)
        for key, value in providers_raw.items()
        if _validate_provider_key(key, issues)
    )
    _validate_provider_keys(providers, issues)
    _validate_target_ids(providers, issues)

    if issues:
        raise RegistryValidationError(issues)
    return Registry(spec_version=spec_version, providers=providers, client_config=client_config)


def _parse_client_config(raw: Any, issues: list[str]) -> dict[str, ClientConfig]:
    clients = _mapping(raw, "registry.clientConfig", issues)
    parsed: dict[str, ClientConfig] = {}
    for logical_client, raw_config in clients.items():
        if not LOGICAL_ID_RE.fullmatch(logical_client):
            issues.append(
                f"registry.clientConfig key {logical_client!r} must be a logical client matching "
                f"{LOGICAL_ID_RE.pattern!r}"
            )
        location = f"registry.clientConfig.{logical_client}"
        config = _mapping(raw_config, location, issues)
        _reject_unknown_fields(config, {"core", "extensions"}, location, issues)
        core: Mapping[str, object] | None = None
        if "core" in config:
            core_value = _nonempty_mapping(config["core"], f"{location}.core", issues)
            core = (
                _parse_factory_core(core_value, f"{location}.core", issues)
                if logical_client == "factory"
                else core_value
            )
        raw_extensions = config.get("extensions", {})
        extensions_value = _mapping(raw_extensions, f"{location}.extensions", issues)
        extensions: dict[str, Mapping[str, object]] = {}
        for extension_name, raw_extension in extensions_value.items():
            extension_location = f"{location}.extensions.{extension_name}"
            if extension_name == "core":
                issues.append(f"{extension_location}: extension name 'core' is reserved")
            elif not LOGICAL_ID_RE.fullmatch(extension_name):
                issues.append(
                    f"{extension_location} extension name must match {LOGICAL_ID_RE.pattern!r}"
                )
            extension_value: Mapping[str, object] = _nonempty_mapping(
                raw_extension, extension_location, issues
            )
            if logical_client == "factory" and extension_name == "oh-my-droid":
                extension_value = _parse_oh_my_droid_extension(
                    extension_value, extension_location, issues
                )
            extensions[extension_name] = extension_value
        parsed[logical_client] = ClientConfig(core=core, extensions=extensions)
    return parsed


def _parse_oh_my_droid_extension(
    value: Mapping[str, Any], location: str, issues: list[str]
) -> Mapping[str, object]:
    _reject_unknown_fields(value, {"droids", "prune"}, location, issues)
    raw_droids = _mapping(value.get("droids"), f"{location}.droids", issues)
    droids: dict[str, object] = {}
    for name, raw_reference in raw_droids.items():
        droid_location = f"{location}.droids.{name}"
        if not isinstance(name, str) or not LOGICAL_ID_RE.fullmatch(name):
            issues.append(f"{droid_location} name must match {LOGICAL_ID_RE.pattern!r}")
        reference = _parse_model_selection(raw_reference, droid_location, False, issues)
        if reference is not None:
            droids[name] = reference
    raw_prune = value.get("prune", False)
    if not isinstance(raw_prune, bool):
        issues.append(f"{location}.prune must be boolean")
        raw_prune = False
    return {"droids": droids, "prune": raw_prune}


def _parse_factory_core(
    value: Mapping[str, Any], location: str, issues: list[str]
) -> Mapping[str, object]:
    _reject_unknown_fields(value, {"defaults", "session", "mission", "subagent"}, location, issues)
    parsed: dict[str, object] = {}
    if "defaults" in value:
        defaults_location = f"{location}.defaults"
        defaults = _mapping(value["defaults"], defaults_location, issues)
        roles = {"worker", "thinker", "orchestrator", "simple", "validator"}
        _reject_unknown_fields(defaults, roles, defaults_location, issues)
        for role in sorted(roles):
            if role not in defaults:
                issues.append(f"{defaults_location}.{role} is required")
            else:
                parsed_reference = _parse_model_selection(
                    defaults[role], f"{defaults_location}.{role}", False, issues
                )
                if parsed_reference is not None:
                    defaults[role] = parsed_reference
        parsed["defaults"] = defaults
    if "session" in value:
        parsed["session"] = _parse_factory_section(
            value["session"],
            f"{location}.session",
            {"model", "specModeModel"},
            {"reasoningEffort", "specModeReasoningEffort"},
            issues,
        )
    if "mission" in value:
        parsed["mission"] = _parse_factory_section(
            value["mission"],
            f"{location}.mission",
            {"orchestratorModel", "workerModel", "validationWorkerModel"},
            {
                "orchestratorReasoningEffort",
                "workerReasoningEffort",
                "validationWorkerReasoningEffort",
            },
            issues,
        )
    if "subagent" in value:
        parsed["subagent"] = _parse_factory_section(
            value["subagent"],
            f"{location}.subagent",
            {"lightModel", "mediumModel", "heavyModel"},
            set(),
            issues,
        )
    return parsed


def _parse_factory_section(
    raw: Any,
    location: str,
    model_fields: set[str],
    effort_fields: set[str],
    issues: list[str],
) -> Mapping[str, object]:
    value = _mapping(raw, location, issues)
    _reject_unknown_fields(value, model_fields | effort_fields, location, issues)
    parsed: dict[str, object] = {}
    for field_name, raw_value in value.items():
        if field_name in model_fields:
            selection = _parse_model_selection(raw_value, f"{location}.{field_name}", True, issues)
            if selection is not None:
                parsed[field_name] = selection
        elif not isinstance(raw_value, str) or raw_value not in REASONING_EFFORTS:
            issues.append(
                f"{location}.{field_name} reasoningEffort must be one of "
                f"{sorted(REASONING_EFFORTS)}"
            )
        else:
            parsed[field_name] = raw_value
    return parsed


def _parse_model_selection(
    raw: Any, location: str, allow_factory_native: bool, issues: list[str]
) -> ModelSelection | None:
    value = _mapping(raw, location, issues)
    if set(value) == {"provider", "model"}:
        provider_key = _required_string(value, "provider", location, issues)
        model_name = _required_string(value, "model", location, issues)
        return ModelReference(provider_key, model_name) if provider_key and model_name else None
    if set(value) == {"factoryNative"}:
        identifier = _required_string(value, "factoryNative", location, issues)
        if not allow_factory_native:
            issues.append(f"{location}.factoryNative is not allowed in Factory defaults")
            return None
        return FactoryNativeReference(identifier) if identifier else None
    issues.append(
        f"{location} must contain exactly provider and model"
        + (" or exactly factoryNative" if allow_factory_native else "")
    )
    return None


def _validate_provider_key(raw: Any, issues: list[str]) -> bool:
    if not isinstance(raw, str) or not raw:
        issues.append("registry.providers key must be a non-empty string")
        return False
    if not PROVIDER_KEY_RE.fullmatch(raw) or "--" in raw:
        issues.append(
            f"registry.providers key {raw!r} must match {PROVIDER_KEY_RE.pattern!r} "
            "and not contain '--'"
        )
        return False
    return True


def _parse_provider(key: str, raw: Any, issues: list[str]) -> Provider:
    location = f"providers.{key}"
    value = _mapping(raw, location, issues)
    allowed = {
        "name",
        "targets",
        "baseUrl",
        "apiKey",
        "enabled",
        "models",
        "provider",
        "extensions",
    }
    _reject_unknown_fields(value, allowed, location, issues)

    name = _required_string(value, "name", location, issues)
    targets = _parse_targets(value.get("targets"), location, issues)
    base_url = _required_string(value, "baseUrl", location, issues)
    _validate_url(base_url, location, issues)
    api_key_reference = _required_string(value, "apiKey", location, issues)
    if api_key_reference and not API_KEY_REFERENCE_RE.fullmatch(api_key_reference):
        issues.append(f"{location}.apiKey must use env.VAR_NAME syntax")
    enabled = _required_bool(value, "enabled", location, issues)
    provider_protocol = _parse_provider_protocol(value.get("provider"), location, issues)
    raw_extensions = value.get("extensions", {})
    extensions = (
        _mapping(raw_extensions, f"{location}.extensions", issues)
        if raw_extensions is not None
        else {}
    )
    _validate_provider_extensions(extensions, location, issues)

    models_raw = value.get("models")
    if not isinstance(models_raw, Mapping) or not models_raw:
        issues.append(f"{location}.models must be a non-empty mapping")
        models_raw = {}
    models = tuple(
        _parse_model(model_key, model_value, location, provider_protocol, issues)
        for model_key, model_value in models_raw.items()
        if _validate_model_key(model_key, location, issues)
    )
    _validate_model_names(models, location, issues)
    return Provider(
        key=key,
        name=name,
        targets=targets,
        base_url=base_url,
        api_key_reference=api_key_reference,
        enabled=enabled,
        provider_protocol=provider_protocol,
        models=models,
        extensions=extensions,
    )


def _validate_model_key(raw: Any, provider_location: str, issues: list[str]) -> bool:
    if not isinstance(raw, str) or not raw:
        issues.append(f"{provider_location}.models key must be a non-empty string")
        return False
    return True


def _parse_model(
    model: str,
    raw: Any,
    provider_location: str,
    provider_protocol: str | None,
    issues: list[str],
) -> Model:
    location = f"{provider_location}.models.{model}"
    value = _mapping(raw, location, issues)
    allowed = {
        "displayName",
        "contextWindow",
        "maxOutputTokens",
        "maxInputTokens",
        "enabled",
        "provider",
        "noImageSupport",
        "toolCalling",
        "favourite",
        "extensions",
    }
    _reject_unknown_fields(value, allowed, location, issues)

    display_name = _required_string(value, "displayName", location, issues)
    context_window = _required_positive_int(value, "contextWindow", location, issues)
    max_output_tokens = _required_positive_int(value, "maxOutputTokens", location, issues)
    if context_window and max_output_tokens and max_output_tokens > context_window:
        issues.append(f"{location}.maxOutputTokens must not exceed contextWindow")
    max_input_tokens = _optional_positive_int(
        value.get("maxInputTokens"), f"{location}.maxInputTokens", issues
    )
    if max_input_tokens is None:
        max_input_tokens = max(context_window - max_output_tokens, 0)
    enabled = _required_bool(value, "enabled", location, issues)
    provider_override = _parse_model_provider(value.get("provider"), location, issues)
    effective_provider = provider_override or provider_protocol or "generic-chat-completion-api"
    no_image_support = _optional_bool(
        value.get("noImageSupport"), f"{location}.noImageSupport", issues
    )
    tool_calling = (
        True
        if "toolCalling" not in value
        else _optional_bool(value.get("toolCalling"), f"{location}.toolCalling", issues)
    )
    favourite = _optional_bool(value.get("favourite"), f"{location}.favourite", issues)
    raw_extensions = value.get("extensions", {})
    extensions = (
        _mapping(raw_extensions, f"{location}.extensions", issues)
        if raw_extensions is not None
        else {}
    )
    _validate_model_extensions(extensions, location, issues)
    chatgpt_reasoning_levels = _parse_chatgpt_reasoning_levels(extensions, location, issues)
    vscode_reasoning_levels, vscode_default_reasoning_level = _parse_vscode_reasoning_levels(
        extensions, location, issues
    )

    return Model(
        model=model,
        display_name=display_name,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        max_input_tokens=max_input_tokens,
        enabled=enabled,
        provider_override=provider_override,
        effective_provider=effective_provider,
        no_image_support=no_image_support,
        tool_calling=tool_calling,
        favourite=favourite,
        extensions=extensions,
        chatgpt_reasoning_levels=chatgpt_reasoning_levels,
        vscode_reasoning_levels=vscode_reasoning_levels,
        vscode_default_reasoning_level=vscode_default_reasoning_level,
    )


def _validate_provider_keys(providers: Sequence[Provider], issues: list[str]) -> None:
    # ponytail: defense-in-depth dup check; DuplicateKeySafeLoader already rejects dup
    # map keys at load time, so this can never fire from parsed input.
    seen: set[str] = set()
    for provider in providers:
        if provider.key in seen:
            issues.append(f"duplicate provider key: {provider.key!r}")
        seen.add(provider.key)


def _validate_model_names(
    models: Sequence[Model], provider_location: str, issues: list[str]
) -> None:
    seen: set[str] = set()
    for model in models:
        if model.model in seen:
            issues.append(f"duplicate model {model.model!r} in {provider_location}")
        seen.add(model.model)


def _validate_target_ids(providers: Sequence[Provider], issues: list[str]) -> None:
    factory_ids: set[str] = set()
    chatgpt_provider_ids: set[str] = set()
    chatgpt_catalog_ids: set[str] = set()
    chatgpt_defaults: list[str] = []
    for provider in providers:
        if provider.emits_to("factory"):
            for model in provider.models:
                if not model.enabled:
                    continue
                factory_id = model.factory_id(provider.key)
                if factory_id in factory_ids:
                    issues.append(f"duplicate effective Factory model id: {factory_id!r}")
                factory_ids.add(factory_id)

        if provider.emits_to("vscode"):
            vscode_ids: set[str] = set()
            for model in provider.models:
                if not model.enabled:
                    continue
                vscode_id = model.vscode_id()
                if vscode_id in vscode_ids:
                    issues.append(
                        f"duplicate effective VS Code model id {vscode_id!r} "
                        f"in provider {provider.key!r}"
                    )
                vscode_ids.add(vscode_id)

        enabled_chatgpt_models = tuple(
            model for model in provider.models if provider.emits_to("chatgpt") and model.enabled
        )
        if provider.chatgpt_default() and not enabled_chatgpt_models:
            issues.append(
                f"provider {provider.key!r} cannot be the ChatGPT default without enabled "
                "ChatGPT models"
            )
        if not enabled_chatgpt_models:
            continue
        if not CHATGPT_PROFILE_KEY_RE.fullmatch(provider.key):
            issues.append(
                f"provider {provider.key!r} must match a Codex profile name "
                f"{CHATGPT_PROFILE_KEY_RE.pattern!r}"
            )
        if provider.chatgpt_default():
            chatgpt_defaults.append(provider.key)
        provider_id = provider.chatgpt_provider_id()
        if provider_id in chatgpt_provider_ids:
            issues.append(f"duplicate effective ChatGPT provider id: {provider_id!r}")
        chatgpt_provider_ids.add(provider_id)
        if provider.chatgpt_wire_api() != "responses" or provider.provider_protocol == "anthropic":
            issues.append(
                f"provider {provider.key!r} must use ChatGPT wireApi 'responses' and must "
                "not use provider-level 'anthropic'"
            )
        for model in enabled_chatgpt_models:
            catalog_id = model.chatgpt_catalog_id()
            if not CHATGPT_CATALOG_ID_RE.fullmatch(catalog_id):
                issues.append(
                    f"provider {provider.key!r} ChatGPT catalog id {catalog_id!r} must be "
                    "non-empty and contain no whitespace or control characters"
                )
            if catalog_id in chatgpt_catalog_ids:
                issues.append(f"duplicate effective ChatGPT catalog id: {catalog_id!r}")
            chatgpt_catalog_ids.add(catalog_id)
    if chatgpt_provider_ids and len(chatgpt_defaults) != 1:
        issues.append("exactly one enabled ChatGPT provider must set extensions.chatgpt.default")


def _validate_provider_extensions(
    extensions: Mapping[str, Any], location: str, issues: list[str]
) -> None:
    _reject_unknown_fields(extensions, {"chatgpt"}, f"{location}.extensions", issues)
    if "chatgpt" not in extensions:
        return
    chatgpt_location = f"{location}.extensions.chatgpt"
    chatgpt_mapping = _mapping(extensions["chatgpt"], chatgpt_location, issues)
    _reject_unknown_fields(
        chatgpt_mapping,
        {"providerId", "wireApi", "default", "httpHeaders"},
        chatgpt_location,
        issues,
    )
    if "httpHeaders" in chatgpt_mapping:
        http_headers = chatgpt_mapping["httpHeaders"]
        if not isinstance(http_headers, Mapping) or not http_headers:
            issues.append(f"{chatgpt_location}.httpHeaders must be a non-empty mapping")
        else:
            for header_key, header_value in http_headers.items():
                if not isinstance(header_key, str) or not header_key:
                    issues.append(f"{chatgpt_location}.httpHeaders keys must be non-empty strings")
                if not isinstance(header_value, str):
                    issues.append(
                        f"{chatgpt_location}.httpHeaders values must be strings "
                        "(codex http_headers contract)"
                    )
    if "default" in chatgpt_mapping and not isinstance(chatgpt_mapping["default"], bool):
        issues.append(f"{chatgpt_location}.default must be a boolean")
    if "providerId" in chatgpt_mapping:
        provider_id = chatgpt_mapping["providerId"]
        if not isinstance(provider_id, str) or not CHATGPT_PROVIDER_ID_RE.fullmatch(provider_id):
            issues.append(
                f"{chatgpt_location}.providerId must match {CHATGPT_PROVIDER_ID_RE.pattern!r}"
            )
    if "wireApi" in chatgpt_mapping and chatgpt_mapping["wireApi"] != "responses":
        issues.append(f"{chatgpt_location}.wireApi must be 'responses'")


def _validate_model_extensions(
    extensions: Mapping[str, Any], location: str, issues: list[str]
) -> None:
    # ponytail: the model-level per-target extension namespaces are thin
    # pass-throughs. `factory.providers` is the Surplus provider-pinning
    # allow-list; `extraArgs`/`extraHeaders` on factory/vscode are unvalidated
    # request passthroughs rendered in each target's native format. Anything
    # outside the declared keys stays rejected (VAL-CATALOG-004).
    _reject_unknown_fields(
        extensions, {"vscode", "chatgpt", "factory"}, f"{location}.extensions", issues
    )
    if "factory" in extensions:
        factory_location = f"{location}.extensions.factory"
        factory_mapping = _mapping(extensions["factory"], factory_location, issues)
        _reject_unknown_fields(
            factory_mapping,
            {"providers", "extraArgs", "extraHeaders"},
            factory_location,
            issues,
        )
        if "providers" in factory_mapping:
            providers = factory_mapping["providers"]
            if (
                not isinstance(providers, list)
                or not providers
                or not all(isinstance(item, str) and item for item in providers)
            ):
                issues.append(
                    f"{factory_location}.providers must be a non-empty list of non-empty strings"
                )
        for passthrough_key in ("extraArgs", "extraHeaders"):
            if passthrough_key in factory_mapping and not isinstance(
                factory_mapping[passthrough_key], Mapping
            ):
                issues.append(f"{factory_location}.{passthrough_key} must be a mapping")
    if "vscode" in extensions:
        vscode_location = f"{location}.extensions.vscode"
        vscode_mapping = _mapping(extensions["vscode"], vscode_location, issues)
        _reject_unknown_fields(
            vscode_mapping,
            {"id", "reasoningLevels", "defaultReasoningLevel", "extraArgs", "extraHeaders"},
            vscode_location,
            issues,
        )
        for passthrough_key in ("extraArgs", "extraHeaders"):
            if passthrough_key in vscode_mapping and not isinstance(
                vscode_mapping[passthrough_key], Mapping
            ):
                issues.append(f"{vscode_location}.{passthrough_key} must be a mapping")
        if "id" in vscode_mapping:
            vscode_id = vscode_mapping["id"]
            if not isinstance(vscode_id, str) or not vscode_id:
                issues.append(f"{vscode_location}.id must be a non-empty string")
        if "defaultReasoningLevel" in vscode_mapping:
            default = vscode_mapping["defaultReasoningLevel"]
            if not isinstance(default, str) or default not in VSCODE_REASONING_EFFORT_SET:
                issues.append(
                    f"{vscode_location}.defaultReasoningLevel must be one of "
                    f"{list(VSCODE_REASONING_EFFORTS)}"
                )
    if "chatgpt" in extensions:
        chatgpt_location = f"{location}.extensions.chatgpt"
        chatgpt_mapping = _mapping(extensions["chatgpt"], chatgpt_location, issues)
        _reject_unknown_fields(
            chatgpt_mapping, {"catalogId", "reasoningLevels"}, chatgpt_location, issues
        )
        if "catalogId" in chatgpt_mapping:
            catalog_id = chatgpt_mapping["catalogId"]
            if not isinstance(catalog_id, str) or not CHATGPT_CATALOG_ID_RE.fullmatch(catalog_id):
                issues.append(
                    f"{chatgpt_location}.catalogId must be non-empty and contain no "
                    "whitespace or control characters"
                )


def _parse_chatgpt_reasoning_levels(
    extensions: Mapping[str, Any], location: str, issues: list[str]
) -> tuple[str, ...]:
    raw_chatgpt = extensions.get("chatgpt")
    if not isinstance(raw_chatgpt, Mapping) or "reasoningLevels" not in raw_chatgpt:
        return ()
    raw_levels = raw_chatgpt["reasoningLevels"]
    field_location = f"{location}.extensions.chatgpt.reasoningLevels"
    if not isinstance(raw_levels, list) or not raw_levels:
        issues.append(f"{field_location} must be a non-empty list")
        return ()
    levels: list[str] = []
    for index, raw_level in enumerate(raw_levels):
        item_location = f"{field_location}[{index}]"
        if not isinstance(raw_level, str) or raw_level not in CHATGPT_REASONING_EFFORT_SET:
            issues.append(f"{item_location} must be one of {list(CHATGPT_REASONING_EFFORTS)}")
            continue
        if raw_level in levels:
            issues.append(f"{field_location} contains duplicate reasoning level {raw_level!r}")
            continue
        levels.append(raw_level)
    return tuple(levels)


def _parse_vscode_reasoning_levels(
    extensions: Mapping[str, Any], location: str, issues: list[str]
) -> tuple[tuple[str, ...], str | None]:
    raw_vscode = extensions.get("vscode")
    if not isinstance(raw_vscode, Mapping) or "reasoningLevels" not in raw_vscode:
        if isinstance(raw_vscode, Mapping) and "defaultReasoningLevel" in raw_vscode:
            issues.append(
                f"{location}.extensions.vscode.defaultReasoningLevel requires reasoningLevels"
            )
        return (), None
    raw_levels = raw_vscode["reasoningLevels"]
    field_location = f"{location}.extensions.vscode.reasoningLevels"
    if not isinstance(raw_levels, list) or not raw_levels:
        issues.append(f"{field_location} must be a non-empty list")
        return (), None
    levels: list[str] = []
    for index, raw_level in enumerate(raw_levels):
        item_location = f"{field_location}[{index}]"
        if not isinstance(raw_level, str) or raw_level not in VSCODE_REASONING_EFFORT_SET:
            issues.append(f"{item_location} must be one of {list(VSCODE_REASONING_EFFORTS)}")
            continue
        if raw_level in levels:
            issues.append(f"{field_location} contains duplicate reasoning level {raw_level!r}")
            continue
        levels.append(raw_level)
    default = raw_vscode.get("defaultReasoningLevel")
    if default is not None and (not isinstance(default, str) or default not in levels):
        issues.append(
            f"{location}.extensions.vscode.defaultReasoningLevel must be one of "
            f"the declared reasoningLevels {levels}"
        )
        default = None
    return tuple(levels), default


def _parse_targets(raw: Any, location: str, issues: list[str]) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        issues.append(f"{location}.targets must be a non-empty list")
        return ()
    targets: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            issues.append(f"{location}.targets[{index}] must be a string")
            continue
        if not LOGICAL_ID_RE.fullmatch(item):
            issues.append(
                f"{location}.targets[{index}] logical client must match {LOGICAL_ID_RE.pattern!r}"
            )
            continue
        if item in targets:
            issues.append(f"{location}.targets contains duplicate target {item!r}")
            continue
        targets.append(item)
    return tuple(targets)


def _parse_provider_protocol(raw: Any, location: str, issues: list[str]) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        issues.append(f"{location}.provider must be a string or null")
        return None
    if not raw.strip():
        issues.append(f"{location}.provider must not be empty")
        return None
    if raw not in PROVIDER_PROTOCOLS:
        issues.append(
            f"{location}.provider-level provider must be one of {sorted(PROVIDER_PROTOCOLS)}"
        )
        return None
    return raw


def _parse_model_provider(raw: Any, location: str, issues: list[str]) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        issues.append(f"{location}.provider must be a string or null")
        return None
    if not raw.strip():
        issues.append(f"{location}.provider must not be empty")
        return None
    return raw


def _validate_url(value: str, location: str, issues: list[str]) -> None:
    if not value:
        return
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.hostname:
        issues.append(f"{location}.baseUrl must be an absolute URL")
        return
    try:
        # Accessing .port validates port syntax and range.
        _ = parsed.port
    except ValueError:
        issues.append(f"{location}.baseUrl must have a valid port")
        return
    if parsed.username is not None or parsed.password is not None:
        issues.append(f"{location}.baseUrl must not include credentials")
        return
    if parsed.fragment:
        issues.append(f"{location}.baseUrl must not include a fragment")
        return
    if parsed.scheme == "https":
        return
    hostname = parsed.hostname.lower()
    loopback = hostname == "localhost" or hostname in {"127.0.0.1", "::1"}
    if parsed.scheme != "http" or not loopback:
        issues.append(f"{location}.baseUrl must use HTTPS unless it targets loopback")


def _mapping(raw: Any, location: str, issues: list[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        issues.append(f"{location} must be a mapping")
        return {}
    if not all(isinstance(key, str) for key in raw):
        issues.append(f"{location} contains a non-string key")
    return {str(key): value for key, value in raw.items()}


def _nonempty_mapping(raw: Any, location: str, issues: list[str]) -> dict[str, Any]:
    value = _mapping(raw, location, issues)
    if isinstance(raw, dict) and not raw:
        issues.append(f"{location} must be a non-empty mapping")
    return value


def _reject_unknown_fields(
    value: Mapping[str, Any], allowed: set[str], location: str, issues: list[str]
) -> None:
    for key in value:
        if key not in allowed:
            issues.append(f"{location} contains unknown field {key!r}")


def _required_string(value: Mapping[str, Any], key: str, location: str, issues: list[str]) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip():
        issues.append(f"{location}.{key} must be a non-empty string")
        return ""
    return raw


def _required_bool(value: Mapping[str, Any], key: str, location: str, issues: list[str]) -> bool:
    raw = value.get(key)
    if type(raw) is not bool:
        issues.append(f"{location}.{key} must be a boolean")
        return False
    return raw


def _required_positive_int(
    value: Mapping[str, Any], key: str, location: str, issues: list[str]
) -> int:
    raw = value.get(key)
    if type(raw) is not int or raw <= 0:
        issues.append(f"{location}.{key} must be a positive integer")
        return 0
    return raw


def _optional_positive_int(raw: Any, location: str, issues: list[str]) -> int | None:
    if raw is None:
        return None
    if type(raw) is not int or raw < 0:
        issues.append(f"{location} must be a non-negative integer")
        return None
    return raw


def _optional_bool(raw: Any, location: str, issues: list[str]) -> bool:
    if raw is None:
        return False
    if type(raw) is not bool:
        issues.append(f"{location} must be a boolean")
        return False
    return raw
