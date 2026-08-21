from __future__ import annotations

import hashlib
import json
import os
import textwrap
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
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
    RuntimeProof,
)
from modfig.clients.chatgpt import (
    ChatGPTConfigError,
    ChatGPTRuntime,
    _project_chatgpt_catalog,
    adapter,
    apply_chatgpt,
    capture_chatgpt_proof_record,
    inspect_chatgpt,
    load_chatgpt_config,
    load_chatgpt_runtime_proof,
    preflight,
    project_chatgpt_catalog,
    project_chatgpt_providers,
    resolve_chatgpt_config_path,
    write_chatgpt_proof_record,
)
from modfig.platform import CapabilityUnavailableError
from modfig.registry import load_registry_text

POSIX_SECURE_IO = pytest.mark.skipif(os.name == "nt", reason="requires native POSIX secure I/O")


def registry_text() -> str:
    return textwrap.dedent(
        """\
        specVersion: "0.1"
        providers:
          router:
            name: Router
            targets: [chatgpt]
            baseUrl: https://router.example/v1
            apiKey: env.ROUTER_KEY
            provider: openai
            enabled: true
            extensions:
              chatgpt:
                default: true
            models:
              enabled-model:
                displayName: Enabled Model
                contextWindow: 8192
                maxOutputTokens: 1024
                enabled: true
              disabled-model:
                displayName: Disabled Model
                contextWindow: 8192
                maxOutputTokens: 1024
                enabled: false
        """
    )


def surplus_registry_text() -> str:
    return textwrap.dedent(
        """\
        specVersion: "0.1"
        providers:
          surplus:
            name: Surplus Intelligence
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


def test_chatgpt_builtin_adapter_contract_fails_closed_before_planning() -> None:
    context = AdapterContext("chatgpt", "core")
    base_identity = ArtifactIdentity("chatgpt-home", PurePosixPath("config.toml"))

    assert isinstance(adapter, AdapterV1)
    assert adapter.describe() == AdapterMetadata("modfig.chatgpt", "chatgpt", "core")
    declaration = adapter.preflight(context)
    assert declaration.proof_requirements == {
        "adapterId": "modfig.chatgpt",
        "logicalClient": "chatgpt",
        "component": "core",
        "runtimeProof": "codex-cli-catalog-v1",
    }
    assert tuple(request.artifact for request in declaration.read_requests) == (base_identity,)
    assert tuple(write.artifact for write in declaration.prospective_writes) == (base_identity,)

    validation = AdapterValidationContext(
        "chatgpt",
        "core",
        lambda reference: load_registry_text(registry_text()).resolve_model(reference, "chatgpt"),
    )
    adapter.validate({}, validation)
    with pytest.raises(AdapterPlanError, match="runtime proof"):
        adapter.plan(
            AdapterPlanContext("chatgpt", "core", {}),
            RuntimeProof({}, "declaration"),
            {},
            {},
        )


def test_chatgpt_preflight_fails_closed_without_catalog_runtime_restart_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODFIG_CHATGPT_PROOF", "/tmp/modfig-missing-chatgpt-proof.json")
    with pytest.raises(
        CapabilityUnavailableError,
        match="ChatGPT.*runtime proof.*unavailable",
    ):
        preflight()


def test_resolve_chatgpt_config_path_uses_codex_home_or_default(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_home = tmp_path / "custom-codex"

    assert resolve_chatgpt_config_path({}, home) == home / ".codex" / "config.toml"
    assert resolve_chatgpt_config_path({"CODEX_HOME": str(codex_home)}, home) == (
        codex_home / "config.toml"
    )


@pytest.mark.parametrize("value", ["relative", "", "."])
def test_resolve_chatgpt_config_path_rejects_unsafe_codex_home(tmp_path: Path, value: str) -> None:
    with pytest.raises(ChatGPTConfigError, match="CODEX_HOME"):
        resolve_chatgpt_config_path({"CODEX_HOME": value}, tmp_path)


def _codex_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    write_config(config, b'token = "credential-sentinel"\n[model_providers]\n')
    executable = tmp_path / "codex-bin"
    executable.write_text("#!/bin/sh\nprintf 'codex 1.2.3\\n'\n", encoding="utf-8")
    executable.chmod(0o700)
    return codex_home, config, executable


def _resolved_chatgpt_model() -> ResolvedModel:
    return ResolvedModel(
        provider_key="router",
        base_url="https://router.example/v1",
        api_key_reference="env.ROUTER_KEY",
        model="enabled-model",
        display_name="Enabled Model",
        max_output_tokens=1024,
        effective_provider="openai",
        no_image_support=False,
        favourite=False,
        factory_id="custom:enabled-model--router",
        chatgpt_provider_id="modfig-router",
        chatgpt_wire_api="responses",
        chatgpt_catalog_id="enabled-model",
        provider_name="Router",
        context_window=8192,
    )


def _catalog_identity() -> ArtifactIdentity:
    return ArtifactIdentity("chatgpt-home", PurePosixPath("modfig-router-catalog.json"))


def _profile_identity(provider_key: str = "router") -> ArtifactIdentity:
    return ArtifactIdentity("chatgpt-home", PurePosixPath(f"{provider_key}.config.toml"))


def _base_identity() -> ArtifactIdentity:
    return ArtifactIdentity("chatgpt-home", PurePosixPath("config.toml"))


def _plan_snapshots(
    source: bytes,
    catalog: bytes | None = None,
    base: bytes | None = None,
) -> dict[ArtifactIdentity, object]:
    return {
        ArtifactIdentity("chatgpt-home", PurePosixPath("router.config.toml")): source,
        _catalog_identity(): AbsentDestination() if catalog is None else catalog,
        _base_identity(): AbsentDestination() if base is None else base,
    }


@POSIX_SECURE_IO
def test_capture_and_load_chatgpt_proof_is_sanitized_and_quiescent(tmp_path: Path) -> None:
    codex_home, config, executable = _codex_fixture(tmp_path)
    proof_path = tmp_path / "proof.json"
    captured = datetime(2026, 8, 7, tzinfo=UTC)

    record = capture_chatgpt_proof_record(
        environ={"CODEX_HOME": str(codex_home)},
        home=tmp_path,
        executable=executable,
        process_probe=lambda: True,
        now=captured,
    )
    write_chatgpt_proof_record(record, proof_path)
    proof = load_chatgpt_runtime_proof(
        proof_path,
        environ={"CODEX_HOME": str(codex_home)},
        home=tmp_path,
        executable=executable,
        process_probe=lambda: True,
        now=captured,
    )

    serialized = proof_path.read_text(encoding="utf-8")
    assert config.read_bytes() == b'token = "credential-sentinel"\n[model_providers]\n'
    assert "credential-sentinel" not in serialized
    assert "containsTomlContents" in serialized
    assert proof.facts["configPath"] == str(config)
    assert proof.provenance is not None


@POSIX_SECURE_IO
def test_chatgpt_proof_capture_allows_missing_modfig_profile(tmp_path: Path) -> None:
    codex_home, config, executable = _codex_fixture(tmp_path)
    config.unlink()

    record = capture_chatgpt_proof_record(
        environ={"CODEX_HOME": str(codex_home)},
        home=tmp_path,
        executable=executable,
        process_probe=lambda: True,
    )

    assert record["config"]["configExists"] is False
    assert not config.exists()


@POSIX_SECURE_IO
def test_chatgpt_proof_capture_fails_closed_when_codex_is_running(tmp_path: Path) -> None:
    codex_home, _config, executable = _codex_fixture(tmp_path)
    with pytest.raises(CapabilityUnavailableError, match="runtime proof"):
        capture_chatgpt_proof_record(
            environ={"CODEX_HOME": str(codex_home)},
            home=tmp_path,
            executable=executable,
            process_probe=lambda: False,
        )


@POSIX_SECURE_IO
def test_load_chatgpt_config_preserves_source_bytes(tmp_path: Path) -> None:
    source = b'# comment\nmodel = "selected"\n'
    path = tmp_path / "config.toml"
    path.write_bytes(source)
    path.chmod(0o600)

    assert load_chatgpt_config(path).source == source


@POSIX_SECURE_IO
def test_chatgpt_adapter_plans_catalog_and_preserves_foreign_config(tmp_path: Path) -> None:
    codex_home, config, executable = _codex_fixture(tmp_path)
    source = chatgpt_config()
    write_config(config, source)
    proof_path = tmp_path / "proof.json"
    write_chatgpt_proof_record(
        capture_chatgpt_proof_record(
            environ={"CODEX_HOME": str(codex_home)},
            home=tmp_path,
            executable=executable,
            process_probe=lambda: True,
            now=datetime(2026, 8, 7, tzinfo=UTC),
        ),
        proof_path,
    )
    proof = load_chatgpt_runtime_proof(
        proof_path,
        environ={"CODEX_HOME": str(codex_home)},
        home=tmp_path,
        executable=executable,
        process_probe=lambda: True,
        now=datetime(2026, 8, 7, tzinfo=UTC),
    )
    context = AdapterPlanContext(
        "chatgpt",
        "core",
        models=(_resolved_chatgpt_model(),),
    )

    plan = adapter.plan(
        context,
        proof,
        _plan_snapshots(source),
        {},
    )

    assert len(plan.artifacts) == 3
    planned = plan.artifacts[0].planned
    assert isinstance(planned, bytes)
    assert b'model = "enabled-model"' in planned
    assert b'model_provider = "modfig-router"' in planned
    assert b"[profiles.work]" not in planned
    assert b"[model_providers.foreign]" in planned
    assert b"[model_providers.modfig-router]" in planned
    assert b'env_key = "ROUTER_KEY"' in planned
    assert b"model_catalog_json = " in planned
    assert "modfig-router" in plan.ownership["providerIds"]
    catalog = plan.artifacts[1].planned
    assert isinstance(catalog, bytes)
    catalog_document = json.loads(catalog.decode("utf-8"))
    assert [entry["slug"] for entry in catalog_document["models"]] == ["enabled-model"]
    assert catalog_document["models"][0]["display_name"] == "Enabled Model [Router]"
    assert (
        plan.ownership["artifactHashes"]["modfig-router-catalog.json"]
        == hashlib.sha256(catalog).hexdigest()
    )
    planned_base = plan.artifacts[2].planned
    assert isinstance(planned_base, bytes)
    assert planned_base.startswith(
        f'model_catalog_json = "{codex_home / "modfig-router-catalog.json"}"\n'.encode()
    )


@POSIX_SECURE_IO
def test_chatgpt_adapter_rejects_unowned_provider_collision(tmp_path: Path) -> None:
    codex_home, config, executable = _codex_fixture(tmp_path)
    source = (
        chatgpt_config()
        + b"""
[model_providers.modfig-router]
name = "Not Router"
"""
    )
    write_config(config, source)
    proof = RuntimeProof(
        {},
        "",
        provenance=ChatGPTRuntime(
            config,
            codex_home,
            executable,
            "sha256:" + "a" * 64,
            "codex 1.2.3",
        ),
    )
    with pytest.raises(AdapterPlanError, match="collision or drift"):
        adapter.plan(
            AdapterPlanContext(
                "chatgpt",
                "core",
                models=(_resolved_chatgpt_model(),),
            ),
            proof,
            _plan_snapshots(source),
            {},
        )


@POSIX_SECURE_IO
def test_chatgpt_adapter_updates_owned_provider_when_catalog_changes(tmp_path: Path) -> None:
    codex_home, config, executable = _codex_fixture(tmp_path)
    source = exact_provider_config()
    write_config(config, source)
    proof = RuntimeProof(
        {},
        "",
        provenance=ChatGPTRuntime(
            config,
            codex_home,
            executable,
            "sha256:" + "a" * 64,
            "codex 1.2.3",
        ),
    )
    original = _resolved_chatgpt_model()
    original_plan = adapter.plan(
        AdapterPlanContext("chatgpt", "core", models=(original,)),
        proof,
        _plan_snapshots(chatgpt_config()),
        {},
    )
    original_profile = original_plan.artifacts[0].planned
    original_catalog = original_plan.artifacts[1].planned
    assert isinstance(original_profile, bytes)
    assert isinstance(original_catalog, bytes)
    added = replace(
        original,
        model="new-model",
        display_name="New Model",
        chatgpt_catalog_id="new-model",
        factory_id="custom:new-model--router",
    )

    plan = adapter.plan(
        AdapterPlanContext("chatgpt", "core", models=(original, added)),
        proof,
        {
            _profile_identity(): original_profile,
            _catalog_identity(): original_catalog,
            _base_identity(): original_profile,
        },
        original_plan.ownership,
    )

    planned = plan.artifacts[0].planned
    assert isinstance(planned, bytes)
    assert b'models = ["enabled-model", "new-model"]' in planned


@POSIX_SECURE_IO
def test_chatgpt_adapter_rejects_selected_model_missing_from_new_provider(tmp_path: Path) -> None:
    codex_home, config, executable = _codex_fixture(tmp_path)
    source = (
        chatgpt_config()
        .replace(
            b'model = "selected-model"',
            b'model = "missing-model"',
        )
        .replace(
            b'model_provider = "foreign"',
            b'model_provider = "modfig-router"',
        )
    )
    write_config(config, source)
    proof = RuntimeProof(
        {},
        "",
        provenance=ChatGPTRuntime(
            config,
            codex_home,
            executable,
            "sha256:" + "a" * 64,
            "codex 1.2.3",
        ),
    )
    with pytest.raises(AdapterPlanError, match="selected model"):
        adapter.plan(
            AdapterPlanContext("chatgpt", "core", models=(_resolved_chatgpt_model(),)),
            proof,
            _plan_snapshots(source),
            {},
        )


@POSIX_SECURE_IO
def test_chatgpt_adapter_isolates_provider_profiles_and_selects_default_for_base(
    tmp_path: Path,
) -> None:
    codex_home, config, executable = _codex_fixture(tmp_path)
    surplus = replace(
        _resolved_chatgpt_model(),
        provider_key="surplus",
        base_url="https://surplus.example/v1",
        api_key_reference="env.SURPLUS_KEY",
        model="gpt-5.6-luna",
        display_name="GPT-5.6 Luna [Surplus]",
        factory_id="custom:gpt-5.6-luna--surplus",
        chatgpt_provider_id="modfig-surplus",
        chatgpt_catalog_id="gpt-5.6-luna",
        provider_name="Surplus",
        chatgpt_default=True,
    )
    openrouter = replace(
        _resolved_chatgpt_model(),
        provider_key="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_reference="env.OPEN_ROUTER_API_KEY",
        model="deepseek/deepseek-v4-pro",
        display_name="DeepSeek V4 Pro [OpenRouter]",
        factory_id="custom:deepseek-deepseek-v4-pro--openrouter",
        chatgpt_provider_id="modfig-openrouter",
        chatgpt_catalog_id="deepseek/deepseek-v4-pro",
        provider_name="OpenRouter",
    )
    source = b'[marketplaces.openai-bundled]\nsource = "local"\n'
    proof = RuntimeProof(
        {},
        "",
        provenance=ChatGPTRuntime(
            config,
            codex_home,
            executable,
            "sha256:" + "a" * 64,
            "codex 1.2.3",
        ),
    )

    plan = adapter.plan(
        AdapterPlanContext("chatgpt", "core", models=(surplus, openrouter)),
        proof,
        {
            _profile_identity("surplus"): AbsentDestination(),
            _profile_identity("openrouter"): AbsentDestination(),
            ArtifactIdentity(
                "chatgpt-home", PurePosixPath("modfig-surplus-catalog.json")
            ): AbsentDestination(),
            ArtifactIdentity(
                "chatgpt-home", PurePosixPath("modfig-openrouter-catalog.json")
            ): AbsentDestination(),
            _base_identity(): source,
        },
        {},
    )

    planned_surplus = plan.artifacts[0].planned
    planned_surplus_catalog = plan.artifacts[1].planned
    planned_openrouter = plan.artifacts[2].planned
    planned_openrouter_catalog = plan.artifacts[3].planned
    planned_base = plan.artifacts[4].planned
    assert isinstance(planned_surplus, bytes)
    assert isinstance(planned_surplus_catalog, bytes)
    assert isinstance(planned_openrouter, bytes)
    assert isinstance(planned_openrouter_catalog, bytes)
    assert isinstance(planned_base, bytes)
    assert b"[model_providers.modfig-surplus]" in planned_surplus
    assert b"[model_providers.modfig-openrouter]" not in planned_surplus
    assert b"[model_providers.modfig-openrouter]" in planned_openrouter
    assert b"[model_providers.modfig-surplus]" not in planned_openrouter
    assert b'model_provider = "modfig-surplus"' in planned_surplus
    assert b'model = "gpt-5.6-luna"' in planned_surplus
    assert b'model_provider = "modfig-openrouter"' in planned_openrouter
    assert b'model = "deepseek/deepseek-v4-pro"' in planned_openrouter
    assert json.loads(planned_surplus_catalog.decode())["models"][0]["slug"] == "gpt-5.6-luna"
    assert json.loads(planned_openrouter_catalog.decode())["models"][0]["slug"] == (
        "deepseek/deepseek-v4-pro"
    )
    assert b"[model_providers.modfig-surplus]" in planned_base
    assert b"[model_providers.modfig-openrouter]" not in planned_base
    assert b'model_provider = "modfig-surplus"' in planned_base
    assert b'model = "gpt-5.6-luna"' in planned_base
    assert b"[marketplaces.openai-bundled]" in planned_base


def two_provider_registry_text() -> str:
    return textwrap.dedent(
        """\
        specVersion: "0.1"
        providers:
          surplus:
            name: Surplus
            targets: [chatgpt]
            baseUrl: https://surplus.example/v1
            apiKey: env.SURPLUS_KEY
            provider: generic-chat-completion-api
            enabled: true
            extensions:
              chatgpt:
                wireApi: responses
                default: true
            models:
              gpt-5.6-sol:
                displayName: GPT-5.6 Sol
                contextWindow: 1000000
                maxOutputTokens: 64000
                enabled: true
          router:
            name: OpenRouter
            targets: [chatgpt]
            baseUrl: https://router.example/v1
            apiKey: env.ROUTER_KEY
            provider: openai
            enabled: true
            models:
              router-sol:
                displayName: GPT-5.6 Sol
                contextWindow: 1000000
                maxOutputTokens: 64000
                enabled: true
        """
    )


def test_project_chatgpt_catalog_labels_provider_and_keeps_wire_ids() -> None:
    catalog = project_chatgpt_catalog(
        load_registry_text(two_provider_registry_text()),
        {"SURPLUS_KEY": "present", "ROUTER_KEY": "present"},
    )

    document = json.loads(catalog.decode("utf-8"))
    assert set(document) == {"models"}
    entries = document["models"]
    assert [entry["slug"] for entry in entries] == ["gpt-5.6-sol", "router-sol"]
    assert entries[0]["display_name"] == "GPT-5.6 Sol [Surplus]"
    assert entries[1]["display_name"] == "GPT-5.6 Sol [OpenRouter]"
    assert entries[0]["context_window"] == 1000000
    assert entries[0]["priority"] == 0
    assert entries[1]["priority"] == 1
    for entry in entries:
        assert entry["visibility"] == "list"
        assert entry["supported_in_api"] is True
        assert entry["supported_reasoning_levels"] == []
        assert "base_instructions" in entry


def test_project_chatgpt_catalog_does_not_double_existing_provider_suffix() -> None:
    labeled = replace(_resolved_chatgpt_model(), display_name="Enabled Model [Router]")
    entries = json.loads(_project_chatgpt_catalog((labeled,)).decode("utf-8"))["models"]
    assert entries[0]["display_name"] == "Enabled Model [Router]"


def test_project_chatgpt_catalog_includes_model_reasoning_levels() -> None:
    model = replace(
        _resolved_chatgpt_model(),
        chatgpt_reasoning_levels=("low", "medium", "high", "xhigh", "max"),
    )

    entries = json.loads(_project_chatgpt_catalog((model,)).decode("utf-8"))["models"]

    assert entries[0]["supported_reasoning_levels"] == [
        {"effort": "low", "description": "Fast responses with lighter reasoning"},
        {
            "effort": "medium",
            "description": "Balances speed and reasoning depth for everyday tasks",
        },
        {"effort": "high", "description": "Greater reasoning depth for complex problems"},
        {"effort": "xhigh", "description": "Extra high reasoning depth for complex problems"},
        {"effort": "max", "description": "Maximum reasoning depth for the hardest problems"},
    ]


def test_project_chatgpt_catalog_hides_images_for_no_image_support_models() -> None:
    catalog = project_chatgpt_catalog(
        load_registry_text(registry_text()),
        {"ROUTER_KEY": "present"},
    )

    entries = json.loads(catalog.decode("utf-8"))["models"]
    assert entries[0]["input_modalities"] == ["text", "image"]

    image_off = replace(
        _resolved_chatgpt_model(),
        no_image_support=True,
    )
    entries = json.loads(_project_chatgpt_catalog((image_off,)).decode("utf-8"))["models"]
    assert entries[0]["input_modalities"] == ["text"]


@POSIX_SECURE_IO
def test_chatgpt_adapter_rejects_unowned_catalog_pointer(tmp_path: Path) -> None:
    codex_home, config, executable = _codex_fixture(tmp_path)
    source = b'model_catalog_json = "/tmp/someone-else.json"\n' + chatgpt_config()
    write_config(config, source)
    proof = RuntimeProof(
        {},
        "",
        provenance=ChatGPTRuntime(
            config,
            codex_home,
            executable,
            "sha256:" + "a" * 64,
            "codex 1.2.3",
        ),
    )
    with pytest.raises(AdapterPlanError, match="unowned.*catalog pointer"):
        adapter.plan(
            AdapterPlanContext("chatgpt", "core", models=(_resolved_chatgpt_model(),)),
            proof,
            _plan_snapshots(source),
            {},
        )


@POSIX_SECURE_IO
def test_chatgpt_adapter_rejects_root_catalog_key(tmp_path: Path) -> None:
    codex_home, config, executable = _codex_fixture(tmp_path)
    source = b'catalog = "someone-else.json"\n' + chatgpt_config()
    write_config(config, source)
    proof = RuntimeProof(
        {},
        "",
        provenance=ChatGPTRuntime(
            config,
            codex_home,
            executable,
            "sha256:" + "a" * 64,
            "codex 1.2.3",
        ),
    )
    with pytest.raises(AdapterPlanError, match="catalog override"):
        adapter.plan(
            AdapterPlanContext("chatgpt", "core", models=(_resolved_chatgpt_model(),)),
            proof,
            _plan_snapshots(source),
            {},
        )


@POSIX_SECURE_IO
def test_chatgpt_adapter_catalog_drift_fails_closed_but_owned_updates_allowed(
    tmp_path: Path,
) -> None:
    codex_home, config, executable = _codex_fixture(tmp_path)
    source = chatgpt_config()
    write_config(config, source)
    proof = RuntimeProof(
        {},
        "",
        provenance=ChatGPTRuntime(
            config,
            codex_home,
            executable,
            "sha256:" + "a" * 64,
            "codex 1.2.3",
        ),
    )
    original = _resolved_chatgpt_model()
    original_plan = adapter.plan(
        AdapterPlanContext("chatgpt", "core", models=(original,)),
        proof,
        _plan_snapshots(source),
        {},
    )
    planned_catalog = original_plan.artifacts[1].planned
    assert isinstance(planned_catalog, bytes)

    # Foreign bytes at the owned catalog path refuse overwrite.
    with pytest.raises(AdapterPlanError, match="catalog drift"):
        adapter.plan(
            AdapterPlanContext("chatgpt", "core", models=(original,)),
            proof,
            _plan_snapshots(source, catalog=b'{"models": []}\n'),
            {},
        )

    # The previously-written ModFig catalog may be replaced after a registry change.
    added = replace(
        original,
        model="new-model",
        display_name="New Model",
        chatgpt_catalog_id="new-model",
        factory_id="custom:new-model--router",
    )
    updated = adapter.plan(
        AdapterPlanContext("chatgpt", "core", models=(original, added)),
        proof,
        _plan_snapshots(source, catalog=planned_catalog),
        original_plan.ownership,
    )
    updated_catalog = updated.artifacts[1].planned
    assert isinstance(updated_catalog, bytes)
    assert json.loads(updated_catalog.decode("utf-8"))["models"][1]["slug"] == "new-model"


@POSIX_SECURE_IO
def test_chatgpt_adapter_plans_base_pointer_without_touching_foreign_tables(
    tmp_path: Path,
) -> None:
    codex_home, config, executable = _codex_fixture(tmp_path)
    source = chatgpt_config()
    write_config(config, source)
    base = (
        b'model = "google/gemini-3.5-flash-lite"\n'
        b'model_provider = "modfig-openrouter"\n\n'
        b'[marketplaces.openai-bundled]\nsource = "local"\n'
    )
    proof = RuntimeProof(
        {},
        "",
        provenance=ChatGPTRuntime(
            config,
            codex_home,
            executable,
            "sha256:" + "a" * 64,
            "codex 1.2.3",
        ),
    )

    plan = adapter.plan(
        AdapterPlanContext("chatgpt", "core", models=(_resolved_chatgpt_model(),)),
        proof,
        _plan_snapshots(source, base=base),
        {},
    )

    planned_base = plan.artifacts[2].planned
    assert isinstance(planned_base, bytes)
    pointer = str(codex_home / "modfig-router-catalog.json")
    assert f'model_catalog_json = "{pointer}"\n'.encode() in planned_base
    assert planned_base.index(b"model_catalog_json = ") < planned_base.index(
        b"[marketplaces.openai-bundled]"
    )
    assert b'model = "enabled-model"' in planned_base
    assert b"[model_providers.modfig-router]" in planned_base
    assert b"[marketplaces.openai-bundled]" in planned_base


@POSIX_SECURE_IO
def test_chatgpt_adapter_rejects_unowned_base_catalog_pointer(tmp_path: Path) -> None:
    codex_home, config, executable = _codex_fixture(tmp_path)
    proof = RuntimeProof(
        {},
        "",
        provenance=ChatGPTRuntime(
            config,
            codex_home,
            executable,
            "sha256:" + "a" * 64,
            "codex 1.2.3",
        ),
    )

    with pytest.raises(AdapterPlanError, match="unowned.*catalog pointer"):
        adapter.plan(
            AdapterPlanContext("chatgpt", "core", models=(_resolved_chatgpt_model(),)),
            proof,
            _plan_snapshots(
                chatgpt_config(),
                base=b'model_catalog_json = "/tmp/someone-else.json"\n',
            ),
            {},
        )


@POSIX_SECURE_IO
def test_chatgpt_adapter_verify_accepts_config_and_catalog_pair(tmp_path: Path) -> None:
    codex_home, config, executable = _codex_fixture(tmp_path)
    catalog_bytes = project_chatgpt_catalog(load_registry_text(registry_text()), {})
    pointer = str(codex_home / "modfig-router-catalog.json")
    written_profile = (
        f'model = "enabled-model"\nmodel_provider = "modfig-router"\n'
        f'model_catalog_json = "{pointer}"\n\n'
        '[model_providers.modfig-router]\nname = "Router"\n'
        'base_url = "https://router.example/v1"\nenv_key = "ROUTER_KEY"\n'
        'wire_api = "responses"\nmodels = ["enabled-model"]\n'
    ).encode()
    written_base = written_profile
    ownership = {
        "artifactHashes": {
            "router.config.toml": hashlib.sha256(written_profile).hexdigest(),
            "modfig-router-catalog.json": hashlib.sha256(catalog_bytes).hexdigest(),
            "config.toml": hashlib.sha256(written_base).hexdigest(),
        },
        "artifactOrder": [
            "router.config.toml",
            "modfig-router-catalog.json",
            "config.toml",
        ],
    }
    proof = RuntimeProof(
        {},
        "",
        provenance=ChatGPTRuntime(
            config,
            codex_home,
            executable,
            "sha256:" + "a" * 64,
            "codex 1.2.3",
        ),
    )

    adapter.verify(
        AdapterContext("chatgpt", "core", models=(_resolved_chatgpt_model(),), ownership=ownership),
        proof,
        (written_profile, catalog_bytes, written_base),
    )


@POSIX_SECURE_IO
def test_chatgpt_adapter_verify_fails_closed_on_divergence_or_shape(tmp_path: Path) -> None:
    codex_home, config, executable = _codex_fixture(tmp_path)
    proof = RuntimeProof(
        {},
        "",
        provenance=ChatGPTRuntime(
            config,
            codex_home,
            executable,
            "sha256:" + "a" * 64,
            "codex 1.2.3",
        ),
    )
    pointer = str(codex_home / "modfig-router-catalog.json")
    written_profile = (
        f'model = "enabled-model"\nmodel_provider = "modfig-router"\n'
        f'model_catalog_json = "{pointer}"\n\n'
        '[model_providers.modfig-router]\nname = "Router"\n'
        'base_url = "https://router.example/v1"\nenv_key = "ROUTER_KEY"\n'
        'wire_api = "responses"\nmodels = ["enabled-model"]\n'
    ).encode()
    written_base = written_profile
    ownership = {
        "artifactHashes": {
            "router.config.toml": hashlib.sha256(written_profile).hexdigest(),
            "modfig-router-catalog.json": hashlib.sha256(
                project_chatgpt_catalog(load_registry_text(registry_text()), {})
            ).hexdigest(),
            "config.toml": hashlib.sha256(written_base).hexdigest(),
        },
        "artifactOrder": [
            "router.config.toml",
            "modfig-router-catalog.json",
            "config.toml",
        ],
    }
    context = AdapterContext(
        "chatgpt", "core", models=(_resolved_chatgpt_model(),), ownership=ownership
    )

    with pytest.raises(AdapterPlanError, match="artifact count"):
        adapter.verify(context, proof, (written_profile,))

    with pytest.raises(AdapterPlanError, match="diverges"):
        adapter.verify(context, proof, (written_profile, b"not-json", written_base))

    with pytest.raises(AdapterPlanError, match="diverges"):
        adapter.verify(
            context,
            proof,
            (
                written_profile,
                b'{"models": [{"slug": "other", "display_name": "Other"}]}\n',
                written_base,
            ),
        )


@POSIX_SECURE_IO
def test_chatgpt_proof_shape_treats_managed_catalog_pointer_as_owned(tmp_path: Path) -> None:
    codex_home, config, executable = _codex_fixture(tmp_path)
    pointer = str(codex_home / "modfig-router-catalog.json")
    write_config(config, f'model_catalog_json = "{pointer}"\n'.encode())

    record = capture_chatgpt_proof_record(
        environ={"CODEX_HOME": str(codex_home)},
        home=tmp_path,
        executable=executable,
        process_probe=lambda: True,
    )
    assert record["config"]["catalogOverrides"] is False

    write_config(config, b'model_catalog_json = "/tmp/someone-else.json"\n')
    record = capture_chatgpt_proof_record(
        environ={"CODEX_HOME": str(codex_home)},
        home=tmp_path,
        executable=executable,
        process_probe=lambda: True,
    )
    assert record["config"]["catalogOverrides"] is True


@POSIX_SECURE_IO
def test_inspect_chatgpt_accepts_managed_catalog_pointer(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    pointer = str(tmp_path / "modfig-router-catalog.json")
    write_config(path, f'model_catalog_json = "{pointer}"\n'.encode() + chatgpt_config())

    inspection = inspect_chatgpt(path, load_registry_text(registry_text()), UnreadableEnvironment())

    assert inspection.active_provider == "foreign"
    assert inspection.planned_provider_ids == ("modfig-router",)
    assert inspection.changed is True


def test_project_chatgpt_providers_emits_only_safe_references_and_enabled_models() -> None:
    registry = load_registry_text(registry_text())
    secret = "do-not-leak-sentinel"

    providers = project_chatgpt_providers(registry, {"ROUTER_KEY": secret})

    assert providers == (
        {
            "id": "modfig-router",
            "name": "Router",
            "base_url": "https://router.example/v1",
            "env_key": "ROUTER_KEY",
            "wire_api": "responses",
            "models": ["enabled-model"],
        },
    )
    assert secret not in repr(providers)


def test_project_chatgpt_providers_allows_factory_generic_with_responses_opt_in() -> None:
    providers = project_chatgpt_providers(
        load_registry_text(surplus_registry_text()),
        {"SURPLUS_API_KEY": "present"},
    )

    assert providers == (
        {
            "id": "modfig-surplus",
            "name": "Surplus Intelligence",
            "base_url": "https://api.surplusintelligence.ai/v1",
            "env_key": "SURPLUS_API_KEY",
            "wire_api": "responses",
            "models": ["gpt-5.6-sol"],
        },
    )


def test_project_chatgpt_providers_does_not_read_environment() -> None:
    providers = project_chatgpt_providers(
        load_registry_text(registry_text()), UnreadableEnvironment()
    )

    assert providers[0]["env_key"] == "ROUTER_KEY"


def test_project_chatgpt_providers_rechecks_responses_transport() -> None:
    registry = load_registry_text(registry_text())
    provider = registry.providers[0]
    object.__setattr__(provider, "provider_protocol", "anthropic")

    with pytest.raises(ChatGPTConfigError, match="responses"):
        project_chatgpt_providers(registry, {"ROUTER_KEY": "present"})


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
def test_project_chatgpt_providers_rejects_unsafe_or_invalid_port_urls(base_url: str) -> None:
    registry = load_registry_text(registry_text())
    provider = registry.providers[0]
    object.__setattr__(provider, "base_url", base_url)

    with pytest.raises(ChatGPTConfigError, match="unsafe base URL"):
        project_chatgpt_providers(registry, {"ROUTER_KEY": "present"})


def chatgpt_config(profile: str | None = None) -> bytes:
    profile_line = f'profile = "{profile}"\n' if profile is not None else ""
    profile_table = ""
    if profile is not None:
        profile_table = textwrap.dedent(
            f"""\

            [profiles.{profile}]
            model_provider = "modfig-router"
            """
        )
    return (
        textwrap.dedent(
            f"""\
            # preserve
            model = "selected-model"
            model_provider = "foreign"
            {profile_line}
            [model_providers.foreign]
            name = "Foreign"
            base_url = "https://foreign.example/v1"
            env_key = "FOREIGN_KEY"
            wire_api = "chat"
            """
        )
        + profile_table
    ).encode()


def write_config(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(0o600)


class UnreadableEnvironment(Mapping[str, str]):
    def __getitem__(self, key: str) -> str:
        raise AssertionError(f"secret environment read: {key}")

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0

    def get(self, key: str, default: Any = None) -> Any:
        raise AssertionError(f"secret environment read: {key}")


def exact_provider_config() -> bytes:
    return textwrap.dedent(
        """\
        model = "selected-model"
        model_provider = "foreign"

        [model_providers.modfig-router]
        name = "Router"
        base_url = "https://router.example/v1"
        env_key = "ROUTER_KEY"
        wire_api = "responses"
        models = ["enabled-model"]
        """
    ).encode()


@POSIX_SECURE_IO
def test_inspect_chatgpt_reports_safe_provider_plan_without_secret_values(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path, chatgpt_config())
    secret = "do-not-leak-sentinel"

    inspection = inspect_chatgpt(
        path,
        load_registry_text(registry_text()),
        UnreadableEnvironment(),
    )

    assert inspection.active_model == "selected-model"
    assert inspection.active_provider == "foreign"
    assert inspection.foreign_provider_ids == ("foreign",)
    assert inspection.managed_provider_ids == ()
    assert inspection.planned_provider_ids == ("modfig-router",)
    assert inspection.selected_profile is None
    assert inspection.profile_override is False
    assert inspection.catalog_supported is True
    assert inspection.requires_catalog_proof is True
    assert inspection.changed is True
    assert secret not in repr(inspection)
    assert secret not in inspection.diff
    assert 'model = "selected-model"' not in inspection.diff
    assert 'model_provider = "foreign"' not in inspection.diff


@POSIX_SECURE_IO
def test_inspect_chatgpt_reports_stale_modfig_provider_as_unowned_change(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(
        path,
        chatgpt_config() + b'\n[model_providers.modfig-stale]\nname = "Candidate"\n',
    )

    with pytest.raises(ChatGPTConfigError, match="collision.*modfig-stale"):
        inspect_chatgpt(path, load_registry_text(registry_text()), UnreadableEnvironment())


@POSIX_SECURE_IO
def test_inspect_chatgpt_rejects_divergent_planned_provider_collision(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(
        path,
        chatgpt_config() + b'\n[model_providers.modfig-router]\nname = "Not Router"\n',
    )

    with pytest.raises(ChatGPTConfigError, match="collision.*modfig-router"):
        inspect_chatgpt(path, load_registry_text(registry_text()), UnreadableEnvironment())


@POSIX_SECURE_IO
def test_inspect_chatgpt_exact_generated_provider_is_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path, exact_provider_config())

    inspection = inspect_chatgpt(path, load_registry_text(registry_text()), UnreadableEnvironment())

    assert inspection.managed_provider_ids == ("modfig-router",)
    assert inspection.changed is False
    assert inspection.diff == ""


@POSIX_SECURE_IO
def test_inspect_chatgpt_rejects_active_stale_provider(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(
        path,
        chatgpt_config().replace(b'model_provider = "foreign"', b'model_provider = "modfig-stale"')
        + b'\n[model_providers.modfig-stale]\nname = "Candidate"\n',
    )

    with pytest.raises(ChatGPTConfigError, match="collision.*modfig-stale"):
        inspect_chatgpt(path, load_registry_text(registry_text()), UnreadableEnvironment())


@POSIX_SECURE_IO
def test_inspect_chatgpt_rejects_selected_profile_non_string_provider(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(
        path,
        b'profile = "work"\n\n[profiles.work]\nmodel_provider = 7\n',
    )

    with pytest.raises(ChatGPTConfigError, match="model_provider"):
        inspect_chatgpt(path, load_registry_text(registry_text()), UnreadableEnvironment())


@POSIX_SECURE_IO
def test_inspect_chatgpt_rejects_malformed_managed_tables(tmp_path: Path) -> None:
    registry = load_registry_text(registry_text())
    cases = (
        b'model_providers = "bad"\n',
        b'profile = "work"\nprofiles = "bad"\n',
        b'profile = "work"\n[profiles]\nwork = "bad"\n',
    )

    for index, content in enumerate(cases):
        path = tmp_path / f"config-{index}.toml"
        write_config(path, content)
        with pytest.raises(ChatGPTConfigError, match="table"):
            inspect_chatgpt(path, registry, UnreadableEnvironment())


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"profile = 7\n", id="profile-non-string"),
        pytest.param(b'profile = "missing"\n', id="profile-missing-table"),
        pytest.param(b"model_provider = 7\n", id="model-provider-non-string"),
        pytest.param(b'[model_providers]\nforeign = "bad"\n', id="provider-entry-scalar"),
    ],
)
@POSIX_SECURE_IO
def test_inspect_chatgpt_rejects_malformed_structure_fail_open(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "config.toml"
    write_config(path, content)

    with pytest.raises(ChatGPTConfigError):
        inspect_chatgpt(path, load_registry_text(registry_text()), UnreadableEnvironment())


@POSIX_SECURE_IO
def test_inspect_chatgpt_rejects_active_missing_provider(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path, b'model_provider = "modfig-router"\n')

    with pytest.raises(ChatGPTConfigError, match="active.*missing|missing.*active"):
        inspect_chatgpt(path, load_registry_text(registry_text()), UnreadableEnvironment())


@pytest.mark.parametrize("override", ["model_providers", "catalog", "model_catalog_json"])
@POSIX_SECURE_IO
def test_inspect_chatgpt_selected_profile_managed_structural_override_fails_closed(
    tmp_path: Path, override: str
) -> None:
    path = tmp_path / "config.toml"
    write_config(
        path,
        f'profile = "work"\n[profiles.work]\n{override} = "value"\n'.encode(),
    )

    with pytest.raises(ChatGPTConfigError, match="profile.*override"):
        inspect_chatgpt(path, load_registry_text(registry_text()), UnreadableEnvironment())


@pytest.mark.parametrize("override", ["catalog", "model_catalog_json"])
@POSIX_SECURE_IO
def test_inspect_chatgpt_top_level_catalog_override_fails_closed(
    tmp_path: Path, override: str
) -> None:
    path = tmp_path / "config.toml"
    write_config(path, f'{override} = "value"\n'.encode())

    with pytest.raises(ChatGPTConfigError, match="catalog.*override"):
        inspect_chatgpt(path, load_registry_text(registry_text()), UnreadableEnvironment())


@POSIX_SECURE_IO
def test_inspect_chatgpt_selected_profile_managed_override_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path, chatgpt_config("work"))

    with pytest.raises(ChatGPTConfigError, match="profile.*override"):
        inspect_chatgpt(path, load_registry_text(registry_text()), {"ROUTER_KEY": "present"})


@POSIX_SECURE_IO
def test_inspect_chatgpt_selected_profile_foreign_provider_is_active_provider(
    tmp_path: Path,
) -> None:
    config = textwrap.dedent(
        """\
            model = "selected-model"
            model_provider = "foreign"
            profile = "work"

            [model_providers.foreign]
            name = "Foreign"
            base_url = "https://foreign.example/v1"
            env_key = "FOREIGN_KEY"
            wire_api = "chat"

            [model_providers.work-foreign]
            name = "Work Foreign"
            base_url = "https://work-foreign.example/v1"
            env_key = "WORK_FOREIGN_KEY"
            wire_api = "chat"

            [profiles.work]
            model_provider = "work-foreign"
            """
    ).encode()
    path = tmp_path / "config.toml"
    write_config(path, config)

    inspection = inspect_chatgpt(path, load_registry_text(registry_text()), UnreadableEnvironment())

    assert inspection.selected_profile == "work"
    assert inspection.active_provider == "work-foreign"


@POSIX_SECURE_IO
def test_inspect_chatgpt_selected_profile_without_provider_falls_back_to_root(
    tmp_path: Path,
) -> None:
    config = textwrap.dedent(
        """\
            model = "selected-model"
            model_provider = "foreign"
            profile = "work"

            [model_providers.foreign]
            name = "Foreign"
            base_url = "https://foreign.example/v1"
            env_key = "FOREIGN_KEY"
            wire_api = "chat"

            [profiles.work]
            model = "work-model"
            """
    ).encode()
    path = tmp_path / "config.toml"
    write_config(path, config)

    inspection = inspect_chatgpt(path, load_registry_text(registry_text()), UnreadableEnvironment())

    assert inspection.selected_profile == "work"
    assert inspection.active_provider == "foreign"


def test_apply_chatgpt_catalog_unproven_fails_before_any_mutation(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    before_bytes = chatgpt_config()
    write_config(path, before_bytes)
    before = path.stat()
    lock_path = tmp_path / ".config.toml.modfig.lock"

    with pytest.raises(CapabilityUnavailableError, match="direct ChatGPT mutation"):
        apply_chatgpt(path, load_registry_text(registry_text()), {"ROUTER_KEY": "present"})

    after = path.stat()
    assert path.read_bytes() == before_bytes
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert not lock_path.exists()
