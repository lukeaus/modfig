from __future__ import annotations

import json
from pathlib import Path

import pytest

from modfig.clients.chatgpt import apply_chatgpt
from modfig.platform import CapabilityUnavailableError
from modfig.registry import load_registry_text

SPEC_DIR = Path(__file__).resolve().parents[3] / "spec"


def test_linux_chatgpt_desktop_remains_unsupported_and_adapter_fails_closed(
    tmp_path: Path,
) -> None:
    matrix = json.loads((SPEC_DIR / "capability-matrix.json").read_text(encoding="utf-8"))
    row = next(
        row
        for row in matrix["rows"]
        if (row["logicalClient"], row["os"], row["surface"]) == ("chatgpt", "linux", "desktop")
    )
    path = tmp_path / "config.toml"
    path.write_bytes(b'model = "unchanged"\n')
    path.chmod(0o600)
    registry = load_registry_text(
        """specVersion: "0.1"
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
      model:
        displayName: Model
        contextWindow: 8192
        maxOutputTokens: 1024
        enabled: true
"""
    )

    assert row["supportStatus"] == "unsupported"
    assert row["unsupportedKind"] == "not-applicable"
    with pytest.raises(CapabilityUnavailableError, match="direct ChatGPT mutation"):
        apply_chatgpt(path, registry, {"ROUTER_KEY": "present"})


def test_macos_chatgpt_desktop_is_home_only_and_profile_unsupported() -> None:
    matrix = json.loads((SPEC_DIR / "capability-matrix.json").read_text(encoding="utf-8"))
    row = next(
        row
        for row in matrix["rows"]
        if (row["logicalClient"], row["os"], row["surface"]) == ("chatgpt", "macos", "desktop")
    )

    assert row["supportStatus"] == "unsupported"
    assert row["profileMode"] == "home-only"
    assert "inherited CODEX_HOME" in row["unsupportedReason"]
    assert "did not select a provider-scoped ModFig profile" in row["unsupportedReason"]
    contract = json.loads(
        (
            SPEC_DIR / "fixtures" / "proof" / "chatgpt" / "chatgpt-desktop-home-only.contract.json"
        ).read_text(encoding="utf-8")
    )
    assert contract["surface"] == "desktop"
    assert contract["configRootResolution"]["observed"] == "inherited-by-bundled-app-server"
    assert contract["profileSelection"] == {
        "namePattern": "<provider-key>",
        "mechanism": "not-observed",
        "status": "not-proven",
    }
    assert contract["observedBaseMarker"] == "rejected"
    assert contract["supportStatus"] == "unsupported"
