from __future__ import annotations

from pathlib import Path

import pytest

from modfig import app
from modfig.errors import AppError


def _registry(provider: str) -> str:
    return f"""specVersion: "0.1"
providers:
  example:
    name: Example
    targets: [factory]
    baseUrl: https://api.example.com/v1
    apiKey: env.EXAMPLE_KEY
    provider: {provider}
    enabled: true
    models:
      example-model:
        displayName: Example Model
        contextWindow: 8192
        maxOutputTokens: 1024
        enabled: true
"""


def test_public_apply_recovers_before_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    manifest = tmp_path / "manifest.json"
    (tmp_path / "pending.json").write_text("pending")
    monkeypatch.setattr(app, "resolve_manifest_path", lambda *_: manifest)
    monkeypatch.setattr(
        app,
        "_recover_pending",
        lambda *args, **kwargs: calls.append("recover") or (_ for _ in ()).throw(AppError("stop")),
    )
    with pytest.raises(AppError, match="stop"):
        app.apply("registry.yaml", "factory", yes=True)
    assert calls == ["recover"]


def test_public_apply_routes_factory_through_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(app, "resolve_manifest_path", lambda *_: manifest)
    monkeypatch.setattr(app, "load_valid_registry", lambda _: object())
    monkeypatch.setattr(app, "load_ownership_manifest_snapshot", lambda _: object())
    monkeypatch.setattr(app, "_selected_apply_clients", lambda *_: ("factory",))
    monkeypatch.setattr(
        app,
        "_apply_transaction",
        lambda *args, **kwargs: calls.append("apply") or (_ for _ in ()).throw(AppError("stop")),
    )
    with pytest.raises(AppError, match="stop"):
        app.apply("registry.yaml", "all", yes=True)
    assert calls == ["apply"]
