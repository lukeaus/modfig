from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from modfig.clients.vscode import (
    VSCodeRuntime,
    _serialize_vscode_settings,
    project_vscode_providers,
)
from modfig.errors import AppError
from modfig.registry import load_registry_text

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
            enabled: true
          routed:
            displayName: Routed
            contextWindow: 8192
            maxOutputTokens: 1024
            enabled: true
    """
)


def test_serialize_vscode_settings_rejects_plaintext_api_key() -> None:
    sentinel = "resolved-secret-sentinel"

    with pytest.raises(AppError, match="credential|apiKey|reference") as error:
        _serialize_vscode_settings({"providers": [{"id": "foreign", "apiKey": sentinel}]})

    assert sentinel not in str(error.value)


def test_serialize_vscode_settings_preserves_environment_api_key_reference() -> None:
    serialized = _serialize_vscode_settings(
        {"providers": [{"id": "ModFig/router", "apiKey": "env.ROUTER_KEY"}]}
    )

    assert json.loads(serialized) == {
        "providers": [{"id": "ModFig/router", "apiKey": "env.ROUTER_KEY"}]
    }


def test_serialize_vscode_settings_normalizes_surrogate_serialization_error() -> None:
    with pytest.raises(AppError, match="Unicode|JSON"):
        _serialize_vscode_settings({"invalid": "\ud800"})


def test_projection_never_reads_sentinel_secrets_during_inspection() -> None:
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
        safe_storage_supported=True,
        key_context="proven",
        process_quiescent=True,
        vendor_api_type_mapping=True,
        vendor_api_type_map={"router": ("openai", "openai")},
    )
    sentinel = "do-not-leak-sentinel"

    providers = project_vscode_providers(registry, runtime)

    assert providers
    first = providers[0]
    assert first["id"] == "ModFig/router"
    assert first["apiKey"] == "env.ROUTER_KEY"
    assert sentinel not in json.dumps(providers)
