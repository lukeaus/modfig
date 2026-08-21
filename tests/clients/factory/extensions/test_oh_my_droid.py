from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

import pytest

from modfig.adapters import (
    AbsentDestination,
    AdapterContext,
    AdapterPlanContext,
    AdapterPlanError,
    AdapterValidationContext,
    ResolvedModel,
)
from modfig.clients.factory.extensions import oh_my_droid
from modfig.components import ExtensionComponent
from modfig.errors import AppError
from modfig.registry import ModelReference, RegistryValidationError, load_registry_text

COMPONENT = ExtensionComponent("oh-my-droid")


def _model() -> ResolvedModel:
    return ResolvedModel(
        provider_key="router",
        base_url="https://router.example/v1",
        api_key_reference="env.ROUTER_KEY",
        model="primary",
        display_name="Primary",
        max_output_tokens=1024,
        effective_provider="openai",
        no_image_support=False,
        favourite=False,
        factory_id="custom:primary--router",
    )


def _fixture_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, bytes]:
    home = tmp_path / "home"
    plugin_root = home / ".factory" / "plugins"
    plugin_droids = plugin_root / "cache" / "oh-my-droid" / "oh-my-droid" / "v1" / "droids"
    user_droids = home / ".factory" / "droids"
    plugin_droids.mkdir(parents=True)
    user_droids.mkdir(parents=True)
    inventory = plugin_root / "installed_plugins.json"
    inventory.write_text(
        json.dumps(
            {"plugins": {"oh-my-droid@oh-my-droid": [{"installPath": str(plugin_droids.parent)}]}}
        ),
        encoding="utf-8",
    )
    inventory.chmod(0o600)
    source = b"---\nname: Analyst\nmodel: old-model\n---\n\n# preserve\n"
    (plugin_droids / "analyst.md").write_bytes(source)
    (plugin_droids / "stale.md").write_text("---\nname: Stale\n---\nbody\n", encoding="utf-8")
    (plugin_droids / "analyst.md").chmod(0o644)
    (plugin_droids / "stale.md").chmod(0o644)
    monkeypatch.setattr(oh_my_droid.Path, "home", staticmethod(lambda: home))
    return home, plugin_droids, source


def _context(config: dict[str, object]) -> AdapterPlanContext:
    model = _model()
    return AdapterPlanContext(
        "factory",
        COMPONENT,
        config,
        (model,),
        lambda reference: (
            model
            if reference == ModelReference("router", "primary")
            else pytest.fail("unexpected model reference")
        ),
    )


def _snapshots(
    home: Path,
    plugin_droids: Path,
    source: bytes,
    *,
    current_analyst: bytes | None = None,
    current_stale: bytes | None = None,
) -> dict[oh_my_droid.ArtifactIdentity, bytes | AbsentDestination]:
    plugin_root = home / ".factory" / "plugins"
    snapshots: dict[oh_my_droid.ArtifactIdentity, bytes | AbsentDestination] = {
        oh_my_droid.ArtifactIdentity(oh_my_droid.PLUGIN_GRANT, oh_my_droid.PLUGIN_INVENTORY): (
            plugin_root / "installed_plugins.json"
        ).read_bytes(),
        oh_my_droid.ArtifactIdentity(
            oh_my_droid.PLUGIN_GRANT,
            PurePosixPath("cache/oh-my-droid/oh-my-droid/v1/droids/analyst.md"),
        ): source,
        oh_my_droid.ArtifactIdentity(
            oh_my_droid.PLUGIN_GRANT,
            PurePosixPath("cache/oh-my-droid/oh-my-droid/v1/droids/stale.md"),
        ): (plugin_droids / "stale.md").read_bytes(),
        oh_my_droid.ArtifactIdentity(
            oh_my_droid.DROID_GRANT, PurePosixPath("analyst.md")
        ): current_analyst or AbsentDestination(),
        oh_my_droid.ArtifactIdentity(
            oh_my_droid.DROID_GRANT, PurePosixPath("stale.md")
        ): current_stale or AbsentDestination(),
    }
    return snapshots


def test_registry_owns_strict_builtin_extension_shape() -> None:
    text = """\
specVersion: "0.1"
providers:
  router:
    name: Router
    targets: [factory]
    baseUrl: https://router.example/v1
    apiKey: env.ROUTER_KEY
    enabled: true
    models:
      primary:
        displayName: Primary
        contextWindow: 8192
        maxOutputTokens: 1024
        enabled: true
clientConfig:
  factory:
    extensions:
      oh-my-droid:
        droids:
          analyst: {provider: router, model: primary}
        prune: false
"""
    registry = load_registry_text(text)
    extension = registry.client_component("factory", COMPONENT)
    assert extension is not None
    assert extension["droids"]["analyst"] == ModelReference("router", "primary")

    with pytest.raises(RegistryValidationError, match="unknown field.*sourcePlugin"):
        load_registry_text(
            text.replace(
                "        droids:\n          analyst: {provider: router, model: primary}\n",
                "        sourcePlugin: old\n",
            )
        )


def test_plan_rewrites_only_frontmatter_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, plugin_droids, source = _fixture_tree(tmp_path, monkeypatch)
    plan = oh_my_droid.adapter.plan(
        _context({"droids": {"analyst": ModelReference("router", "primary")}, "prune": False}),
        None,
        _snapshots(home, plugin_droids, source),
        {},
    )
    assert len(plan.artifacts) == 1
    assert plan.artifacts[0].planned == (
        b"---\nname: Analyst\nmodel: custom:primary--router\n---\n\n# preserve\n"
    )


def test_per_plugin_inventory_files_are_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, plugin_droids, _source = _fixture_tree(tmp_path, monkeypatch)
    metadata_root = home / ".factory" / "plugins" / "installed_plugins"
    metadata_root.mkdir()
    metadata = metadata_root / "oh-my-droid-oh-my-droid-user.json"
    metadata.write_text(
        json.dumps(
            {
                "pluginId": "oh-my-droid@oh-my-droid",
                "entry": {"installPath": str(plugin_droids.parent)},
            }
        ),
        encoding="utf-8",
    )
    metadata.chmod(0o644)

    context = AdapterValidationContext(
        "factory",
        COMPONENT,
        lambda reference: (
            _model()
            if reference == ModelReference("router", "primary")
            else pytest.fail("unexpected model reference")
        ),
    )
    config = {"droids": {"analyst": ModelReference("router", "primary")}, "prune": False}
    oh_my_droid.adapter.validate(config, context)
    declaration = oh_my_droid.adapter.preflight(AdapterContext("factory", COMPONENT))

    assert tuple(request.artifact.relative_path for request in declaration.read_requests) == (
        PurePosixPath("installed_plugins/oh-my-droid-oh-my-droid-user.json"),
        PurePosixPath("cache/oh-my-droid/oh-my-droid/v1/droids/analyst.md"),
        PurePosixPath("cache/oh-my-droid/oh-my-droid/v1/droids/stale.md"),
    )


def test_missing_inventory_uses_preflight_droid_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, plugin_droids, source = _fixture_tree(tmp_path, monkeypatch)
    snapshots = _snapshots(home, plugin_droids, source)
    snapshots[
        oh_my_droid.ArtifactIdentity(oh_my_droid.PLUGIN_GRANT, oh_my_droid.PLUGIN_INVENTORY)
    ] = AbsentDestination()
    config = {"droids": {"analyst": ModelReference("router", "primary")}, "prune": False}
    plugin_root = home / ".factory" / "plugins"
    (plugin_root / "installed_plugins.json").unlink()
    for path in plugin_droids.iterdir():
        path.unlink()
    plugin_droids.rmdir()

    plan = oh_my_droid.adapter.plan(_context(config), None, snapshots, {})

    assert plan.artifacts[0].planned == (
        b"---\nname: Analyst\nmodel: custom:primary--router\n---\n\n# preserve\n"
    )


def test_present_inventory_uses_preflight_droid_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, plugin_droids, source = _fixture_tree(tmp_path, monkeypatch)
    snapshots = _snapshots(home, plugin_droids, source)
    config = {"droids": {"analyst": ModelReference("router", "primary")}, "prune": False}
    for path in plugin_droids.iterdir():
        path.unlink()
    plugin_droids.rmdir()

    plan = oh_my_droid.adapter.plan(_context(config), None, snapshots, {})

    assert plan.artifacts[0].planned == (
        b"---\nname: Analyst\nmodel: custom:primary--router\n---\n\n# preserve\n"
    )


def test_oversized_plugin_droid_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, plugin_droids, _source = _fixture_tree(tmp_path, monkeypatch)
    oversized = plugin_droids / "analyst.md"
    with oversized.open("wb") as handle:
        handle.truncate(16 * 1024 * 1024 + 1)

    with pytest.raises(AdapterPlanError, match="exceeds 16 MiB"):
        oh_my_droid.adapter.preflight(AdapterContext("factory", COMPONENT))


def test_frontmatter_rewrite_preserves_crlf_bytes() -> None:
    source = b"---\r\nname: Analyst\r\nmodel: old\r\n---\r\nbody\r\n"
    assert oh_my_droid._rewrite_model_frontmatter(source, "analyst", "new") == (
        b"---\r\nname: Analyst\r\nmodel: new\r\n---\r\nbody\r\n"
    )


def test_retained_owned_droid_keeps_hash_when_prune_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, plugin_droids, source = _fixture_tree(tmp_path, monkeypatch)
    stale = (plugin_droids / "stale.md").read_bytes()
    plan = oh_my_droid.adapter.plan(
        _context({"droids": {"analyst": ModelReference("router", "primary")}, "prune": False}),
        None,
        _snapshots(home, plugin_droids, source, current_stale=stale),
        {
            "droidNames": ["stale"],
            "droidHashes": {"stale": hashlib.sha256(stale).hexdigest()},
        },
    )
    assert plan.ownership["droidNames"] == ("analyst", "stale")
    assert plan.ownership["droidHashes"]["stale"] == hashlib.sha256(stale).hexdigest()


def test_configured_unowned_destination_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, plugin_droids, source = _fixture_tree(tmp_path, monkeypatch)
    foreign = b"foreign droid\n"
    (home / ".factory" / "droids" / "analyst.md").write_bytes(foreign)
    plan = oh_my_droid.adapter.plan(
        _context({"droids": {"analyst": ModelReference("router", "primary")}, "prune": False}),
        None,
        _snapshots(home, plugin_droids, source, current_analyst=foreign),
        {},
    )

    assert plan.artifacts[0].planned == (
        b"---\nname: Analyst\nmodel: custom:primary--router\n---\n\n# preserve\n"
    )
    assert plan.ownership["pluginDerivedNames"] == ()


def test_malformed_present_inventory_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _plugin_droids, _source = _fixture_tree(tmp_path, monkeypatch)
    inventory = home / ".factory" / "plugins" / "installed_plugins.json"
    inventory.write_text("{not-json", encoding="utf-8")
    with pytest.raises(AdapterPlanError, match="inventory is malformed"):
        oh_my_droid.adapter.preflight(AdapterContext("factory", COMPONENT))


def test_owner_readable_destination_can_be_snapshotted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _plugin_droids, _source = _fixture_tree(tmp_path, monkeypatch)
    destination = home / ".factory" / "droids" / "analyst.md"
    destination.write_text("legacy\n", encoding="utf-8")
    destination.chmod(0o644)
    content, version = oh_my_droid.snapshot_droid_file(destination)
    assert content == b"legacy\n"
    assert version.exists is True


def test_prune_deletes_only_owned_plugin_derived_droids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, plugin_droids, source = _fixture_tree(tmp_path, monkeypatch)
    stale = (plugin_droids / "stale.md").read_bytes()
    user_stale = b"---\nname: Stale\nmodel: custom:other\n---\nuser edit\n"
    (home / ".factory" / "droids" / "stale.md").write_bytes(user_stale)
    plan = oh_my_droid.adapter.plan(
        _context({"droids": {"analyst": ModelReference("router", "primary")}, "prune": True}),
        None,
        _snapshots(
            home,
            plugin_droids,
            source,
            current_stale=user_stale,
        ),
        {
            "droidNames": ["stale"],
            "droidHashes": {"stale": hashlib.sha256(user_stale).hexdigest()},
            "pluginDerivedNames": ["stale"],
        },
    )
    assert [artifact.artifact.relative_path for artifact in plan.artifacts] == [
        PurePosixPath("analyst.md"),
        PurePosixPath("stale.md"),
    ]
    assert isinstance(plan.artifacts[1].planned, AbsentDestination)
    assert stale != user_stale


def test_prune_preserves_configured_unowned_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, plugin_droids, source = _fixture_tree(tmp_path, monkeypatch)
    foreign = b"foreign droid\n"
    (home / ".factory" / "droids" / "analyst.md").write_bytes(foreign)
    first = oh_my_droid.adapter.plan(
        _context({"droids": {"analyst": ModelReference("router", "primary")}, "prune": False}),
        None,
        _snapshots(home, plugin_droids, source, current_analyst=foreign),
        {},
    )
    generated = first.artifacts[0].planned
    assert isinstance(generated, bytes)

    plan = oh_my_droid.adapter.plan(
        _context({"droids": {"stale": ModelReference("router", "primary")}, "prune": True}),
        None,
        _snapshots(home, plugin_droids, source, current_analyst=generated),
        first.ownership,
    )

    assert [artifact.artifact.relative_path for artifact in plan.artifacts] == [
        PurePosixPath("stale.md")
    ]
    assert plan.ownership["pluginDerivedNames"] == ("stale",)


def test_inventory_metadata_file_count_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _plugin_droids, _source = _fixture_tree(tmp_path, monkeypatch)
    metadata_root = home / ".factory" / "plugins" / "installed_plugins"
    metadata_root.mkdir()
    for index in range(oh_my_droid._MAX_PLUGIN_METADATA_FILES + 1):
        metadata = metadata_root / f"plugin-{index}.json"
        metadata.write_text("{}", encoding="utf-8")
        metadata.chmod(0o644)

    with pytest.raises(AppError, match="too many metadata files"):
        oh_my_droid.adapter.preflight(AdapterContext("factory", COMPONENT))


def test_inventory_metadata_bytes_are_bounded_during_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _plugin_droids, _source = _fixture_tree(tmp_path, monkeypatch)
    metadata_root = home / ".factory" / "plugins" / "installed_plugins"
    metadata_root.mkdir()
    monkeypatch.setattr(oh_my_droid, "_MAX_PLUGIN_METADATA_BYTES", 100)
    metadata = {
        "pluginId": "oh-my-droid@oh-my-droid",
        "entry": {"installPath": str(home / ".factory" / "plugins" / "cache")},
    }
    for index in range(2):
        path = metadata_root / f"plugin-{index}.json"
        path.write_text(json.dumps(metadata), encoding="utf-8")
        path.chmod(0o644)

    with pytest.raises(AppError, match="exceeds 16 MiB"):
        oh_my_droid.adapter.preflight(AdapterContext("factory", COMPONENT))


def test_inventory_metadata_bytes_are_bounded_during_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, plugin_droids, source = _fixture_tree(tmp_path, monkeypatch)
    snapshots = _snapshots(home, plugin_droids, source)
    snapshots.pop(
        oh_my_droid.ArtifactIdentity(oh_my_droid.PLUGIN_GRANT, oh_my_droid.PLUGIN_INVENTORY)
    )
    metadata_identity = oh_my_droid.ArtifactIdentity(
        oh_my_droid.PLUGIN_GRANT,
        PurePosixPath("installed_plugins/plugin.json"),
    )
    snapshots[metadata_identity] = b"{}"
    snapshots[
        oh_my_droid.ArtifactIdentity(
            oh_my_droid.PLUGIN_GRANT,
            PurePosixPath("installed_plugins/other.json"),
        )
    ] = b"{}"
    monkeypatch.setattr(oh_my_droid, "_MAX_PLUGIN_METADATA_BYTES", 3)

    with pytest.raises(AdapterPlanError, match="exceeds 16 MiB"):
        oh_my_droid.adapter.plan(
            _context({"droids": {"analyst": ModelReference("router", "primary")}, "prune": False}),
            None,
            snapshots,
            {},
        )


def test_validate_names_missing_plugin_droid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture_tree(tmp_path, monkeypatch)
    context = AdapterValidationContext(
        "factory",
        COMPONENT,
        lambda reference: _model(),
    )
    with pytest.raises(AdapterPlanError, match="missing"):
        oh_my_droid.adapter.validate(
            {"droids": {"missing": ModelReference("router", "primary")}, "prune": False},
            context,
        )
