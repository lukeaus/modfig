from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath

import pytest

from modfig import app
from modfig.adapter_routes import AdapterRoute, PathGrant
from modfig.adapters import (
    AdapterContext,
    AdapterMetadata,
    AdapterPlanError,
    ArtifactIdentity,
    ArtifactPlan,
    PlannedArtifact,
    RuntimeProof,
    load_enabled_adapter,
    preflight_declaration_sha256,
)
from modfig.components import ExtensionComponent
from modfig.manifest import (
    AdapterProvenance,
    ClientOwnership,
    ComponentOwnership,
    OwnershipManifest,
    ownership_manifest_bytes,
)

POSIX_SECURE_IO = pytest.mark.skipif(os.name == "nt", reason="requires native POSIX secure I/O")

_EXTERNAL_CONFIG = """specVersion: "0.1"
providers:
  router:
    name: Router
    targets: [factory]
    baseUrl: https://router.example/v1
    apiKey: env.ROUTER_KEY
    provider: openai
    enabled: false
    models:
      primary:
        displayName: Primary
        contextWindow: 8192
        maxOutputTokens: 1024
        enabled: true
clientConfig:
  factory:
    extensions:
      helper:
        sourcePlugin: helper@helper
        droids:
          analyst: {provider: router, model: primary}
"""
_FACTORY_PLUS_EXTERNAL_CONFIG = """specVersion: "0.1"
providers:
  router:
    name: Router
    targets: [factory]
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
    extensions:
      helper:
        sourcePlugin: helper@helper
        droids:
          analyst: {provider: router, model: primary}
"""


class _Distribution:
    def __init__(self, name: str, entry_points: list[object], version: str = "1.0.0") -> None:
        self.name = name
        self.entry_points = entry_points
        self.version = version


class _EntryPoint:
    def __init__(self, adapter: object) -> None:
        self.name = "io.example.helper"
        self.group = "modfig.adapters.v1"
        self.value = "oh_my_droid_adapter:adapter"
        self.dist = _Distribution("example-helper-adapter", [self])
        self._adapter = adapter

    def load(self) -> object:
        return self._adapter


def _load_fixture_adapter() -> object:
    path = Path(__file__).parent / "fixtures" / "external_adapters" / "oh_my_droid_adapter.py"
    spec = importlib.util.spec_from_file_location("oh_my_droid_adapter", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.adapter


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def _external_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    configured: bool = True,
    directory_write_grant: bool = False,
) -> tuple[Path, Path, Path, object, RuntimeProof]:
    config = tmp_path / "modfig.yaml"
    _write_text(
        config, _EXTERNAL_CONFIG if configured else _EXTERNAL_CONFIG.split("clientConfig:")[0]
    )
    source = tmp_path / "source.json"
    _write_text(source, '{"plugin":"before"}')
    destination = tmp_path / "rendered.json"
    manifest = tmp_path / "manifest.json"
    component = ExtensionComponent("helper")
    write_grant = (
        PathGrant("plugin-write", "directory", tmp_path, PurePosixPath("."))
        if directory_write_grant
        else PathGrant("plugin-write", "file", destination, None)
    )
    route = AdapterRoute(
        "factory",
        component,
        "io.example.helper",
        "example-helper-adapter",
        True,
        (PathGrant("plugin-read", "file", source, None),),
        (write_grant,),
    )
    adapter = _load_fixture_adapter()
    declaration = adapter.preflight(AdapterContext("factory", component))
    proof = RuntimeProof({"external": "proof"}, preflight_declaration_sha256(declaration))
    entry_point = _EntryPoint(adapter)
    monkeypatch.setattr(app, "resolve_manifest_path", lambda *_: manifest)
    builtin_route = AdapterRoute(
        "factory",
        "core",
        "modfig.factory",
        "modfig",
        True,
        (),
        (),
        True,
    )
    monkeypatch.setattr(
        app,
        "_merged_adapter_routes",
        lambda **kwargs: app.AdapterRoutes((builtin_route, route)),
    )
    monkeypatch.setattr(
        app, "discover_adapter_entry_points", lambda: {entry_point.name: entry_point}
    )
    return (
        config,
        destination,
        manifest,
        None,
        proof,
    )


def test_external_extension_binds_to_exact_route_component(tmp_path: Path) -> None:
    component = ExtensionComponent("helper")
    route = AdapterRoute(
        "factory",
        component,
        "io.example.helper",
        "example-helper-adapter",
        True,
        (PathGrant("plugin-read", "file", tmp_path / "source.json", None),),
        (PathGrant("plugin-write", "file", tmp_path / "rendered.json", None),),
    )
    adapter = _load_fixture_adapter()

    loaded = load_enabled_adapter(route, entry_points={route.adapter_id: _EntryPoint(adapter)})

    assert loaded.describe() == AdapterMetadata("io.example.helper", "factory", component)


@POSIX_SECURE_IO
def test_manifest_only_external_extension_removes_unchanged_owned_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, destination, manifest, key, proof = _external_harness(
        tmp_path, monkeypatch, configured=False
    )
    component = ExtensionComponent("helper")
    original = b'{"renderer":"owned"}'
    _write_text(destination, original.decode())
    record = ComponentOwnership(
        component,
        AdapterProvenance("io.example.helper", "example-helper-adapter"),
        "plugin-write",
        PurePosixPath("rendered.json"),
        None,
        hashlib.sha256(original).hexdigest(),
        {"renderer": "fake-helper"},
    )
    manifest.write_bytes(
        ownership_manifest_bytes(OwnershipManifest(clients={"factory": ClientOwnership((record,))}))
    )
    manifest.chmod(0o600)

    app._apply_transaction(
        str(config),
        "factory",
        True,
        {("factory", component): proof},
        journal_path=tmp_path / "pending.json",
        backup_root=tmp_path / "backups",
    )

    assert not destination.exists()
    assert "factory" not in app.load_ownership_manifest(manifest).clients


@POSIX_SECURE_IO
def test_external_adapter_receives_only_selected_extension_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, destination, manifest, key, proof = _external_harness(tmp_path, monkeypatch)
    component = ExtensionComponent("helper")

    app._apply_transaction(
        str(config),
        "factory",
        True,
        {("factory", component): proof},
        journal_path=tmp_path / "pending.json",
        backup_root=tmp_path / "backups",
    )

    output = json.loads(destination.read_text(encoding="utf-8"))
    assert output["config"] == {
        "droids": {"analyst": {"model": "primary", "provider": "router"}},
        "sourcePlugin": "helper@helper",
    }
    assert output["sourceSha256"] == hashlib.sha256(b'{"plugin":"before"}').hexdigest()
    record = app.load_ownership_manifest(manifest).clients["factory"].components[0]
    assert record.component == component
    assert dict(record.ownership) == {"renderer": "fake-helper"}


@POSIX_SECURE_IO
def test_external_manifest_record_persists_distribution_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _destination, manifest, key, proof = _external_harness(tmp_path, monkeypatch)
    component = ExtensionComponent("helper")

    app._apply_transaction(
        str(config),
        "factory",
        True,
        {("factory", component): proof},
        journal_path=tmp_path / "pending.json",
        backup_root=tmp_path / "backups",
    )
    record = app.load_ownership_manifest(manifest).clients["factory"].components[0]
    assert record.adapter.version == "1.0.0"


@POSIX_SECURE_IO
def test_versioned_external_manifest_record_reconciles_with_same_distribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, destination, manifest, key, proof = _external_harness(tmp_path, monkeypatch)
    component = ExtensionComponent("helper")

    app._apply_transaction(
        str(config),
        "factory",
        True,
        {("factory", component): proof},
        journal_path=tmp_path / "pending.json",
        backup_root=tmp_path / "backups",
    )
    _write_text(config, _EXTERNAL_CONFIG.split("clientConfig:")[0])

    app._apply_transaction(
        str(config),
        "factory",
        True,
        {("factory", component): proof},
        journal_path=tmp_path / "pending.json",
        backup_root=tmp_path / "backups",
    )

    assert not destination.exists()
    assert "factory" not in app.load_ownership_manifest(manifest).clients


@POSIX_SECURE_IO
def test_external_extension_manual_edit_blocks_cleanup_before_backup_or_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, destination, manifest, key, proof = _external_harness(
        tmp_path, monkeypatch, configured=False
    )
    component = ExtensionComponent("helper")
    original = b'{"renderer":"owned"}'
    _write_text(destination, original.decode())
    record = ComponentOwnership(
        component,
        AdapterProvenance("io.example.helper", "example-helper-adapter"),
        "plugin-write",
        PurePosixPath("rendered.json"),
        None,
        hashlib.sha256(original).hexdigest(),
        {"renderer": "fake-helper"},
    )
    manifest.write_bytes(
        ownership_manifest_bytes(OwnershipManifest(clients={"factory": ClientOwnership((record,))}))
    )
    manifest.chmod(0o600)
    _write_text(destination, "user edit\n")
    journal = tmp_path / "pending.json"
    backups = tmp_path / "backups"

    with pytest.raises(app.AppError, match="external owned artifact has drifted"):
        app._apply_transaction(
            str(config),
            "factory",
            True,
            {("factory", component): proof},
            journal_path=journal,
            backup_root=backups,
        )

    assert destination.read_text(encoding="utf-8") == "user edit\n"
    assert manifest.exists()
    assert not journal.exists()
    assert not backups.exists()


@POSIX_SECURE_IO
def test_missing_external_owned_artifact_blocks_cleanup_before_backup_or_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, destination, manifest, key, proof = _external_harness(
        tmp_path, monkeypatch, configured=False
    )
    component = ExtensionComponent("helper")
    original = b'{"renderer":"owned"}'
    record = ComponentOwnership(
        component,
        AdapterProvenance("io.example.helper", "example-helper-adapter"),
        "plugin-write",
        PurePosixPath("rendered.json"),
        None,
        hashlib.sha256(original).hexdigest(),
        {"renderer": "fake-helper"},
    )
    manifest.write_bytes(
        ownership_manifest_bytes(OwnershipManifest(clients={"factory": ClientOwnership((record,))}))
    )
    manifest.chmod(0o600)
    journal = tmp_path / "pending.json"
    backups = tmp_path / "backups"

    with pytest.raises(app.AppError, match="external owned artifact has drifted"):
        app._apply_transaction(
            str(config),
            "factory",
            True,
            {("factory", component): proof},
            journal_path=journal,
            backup_root=backups,
        )

    assert not destination.exists()
    assert manifest.exists()
    assert not journal.exists()
    assert not backups.exists()


@POSIX_SECURE_IO
def test_external_verification_failure_rolls_back_factory_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "modfig.yaml"
    _write_text(config, _FACTORY_PLUS_EXTERNAL_CONFIG)
    settings = tmp_path / "settings.json"
    before_settings = b'{"customModels":[],"modelFavorites":[]}'
    _write_text(settings, before_settings.decode())
    source = tmp_path / "source.json"
    _write_text(source, '{"plugin":"before"}')
    destination = tmp_path / "rendered.json"
    manifest = tmp_path / "manifest.json"
    component = ExtensionComponent("helper")
    factory_grant = PathGrant("factory-config", "file", settings, None)
    external_route = AdapterRoute(
        "factory",
        component,
        "io.example.helper",
        "example-helper-adapter",
        True,
        (PathGrant("plugin-read", "file", source, None),),
        (PathGrant("plugin-write", "directory", tmp_path, PurePosixPath(".")),),
    )
    external_adapter = _load_fixture_adapter()
    external_declaration = external_adapter.preflight(AdapterContext("factory", component))
    external_proof = RuntimeProof(
        {"external": "proof"}, preflight_declaration_sha256(external_declaration)
    )

    def fail_verify(*args: object) -> None:
        del args
        raise app.AppError("external verification failed")

    monkeypatch.setattr(external_adapter, "verify", fail_verify)
    entry_point = _EntryPoint(external_adapter)
    factory_route = AdapterRoute(
        "factory",
        "core",
        "modfig.factory",
        "modfig",
        True,
        (factory_grant,),
        (factory_grant,),
        True,
    )
    monkeypatch.setattr(app, "resolve_manifest_path", lambda *_: manifest)
    monkeypatch.setattr(
        app,
        "_merged_adapter_routes",
        lambda **kwargs: app.AdapterRoutes((factory_route, external_route)),
    )
    monkeypatch.setattr(
        app, "discover_adapter_entry_points", lambda: {entry_point.name: entry_point}
    )
    # ponytail: this test exercises external-adapter rollback downstream of the
    # live Responses probe; the probe has dedicated coverage in test_probe.py.
    monkeypatch.setattr(app.factory, "probe_factory_models", lambda *args, **kwargs: None)
    journal = tmp_path / "pending.json"
    backups = tmp_path / "backups"

    with pytest.raises(app.AppError, match="external verification failed"):
        app._apply_transaction(
            str(config),
            "factory",
            True,
            {("factory", component): external_proof},
            journal_path=journal,
            backup_root=backups,
        )

    assert settings.read_bytes() == before_settings
    assert not destination.exists()
    assert not manifest.exists()
    assert not journal.exists()
    assert not backups.exists() or list(backups.iterdir()) == []


@POSIX_SECURE_IO
def test_external_extension_rejects_plan_artifact_outside_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, destination, manifest, key, proof = _external_harness(tmp_path, monkeypatch)
    component = ExtensionComponent("helper")
    adapter = _load_fixture_adapter()
    original_plan = adapter.plan

    def plan_with_undeclared_artifact(*args: object) -> ArtifactPlan:
        planned = original_plan(*args)
        return ArtifactPlan(
            (
                PlannedArtifact(
                    ArtifactIdentity("plugin-write", PurePosixPath("other.json")),
                    planned.artifacts[0].planned,
                    "features.helper.render",
                    {},
                ),
            ),
            planned.ownership,
        )

    monkeypatch.setattr(adapter, "plan", plan_with_undeclared_artifact)
    entry_point = _EntryPoint(adapter)
    monkeypatch.setattr(
        app, "discover_adapter_entry_points", lambda: {entry_point.name: entry_point}
    )

    with pytest.raises(AdapterPlanError, match="prospective write"):
        app._apply_transaction(
            str(config),
            "factory",
            True,
            {("factory", component): proof},
            journal_path=tmp_path / "pending.json",
            backup_root=tmp_path / "backups",
        )


@POSIX_SECURE_IO
def test_external_adapter_cannot_read_undeclared_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, destination, manifest, key, proof = _external_harness(tmp_path, monkeypatch)
    component = ExtensionComponent("helper")
    adapter = _load_fixture_adapter()
    original_plan = adapter.plan

    def plan_reading_undeclared_source(*args: object) -> ArtifactPlan:
        plan_context, runtime_proof, _snapshots, ownership = args
        return original_plan(plan_context, runtime_proof, {}, ownership)

    monkeypatch.setattr(adapter, "plan", plan_reading_undeclared_source)
    entry_point = _EntryPoint(adapter)
    monkeypatch.setattr(
        app, "discover_adapter_entry_points", lambda: {entry_point.name: entry_point}
    )

    with pytest.raises(KeyError):
        app._apply_transaction(
            str(config),
            "factory",
            True,
            {("factory", component): proof},
            journal_path=tmp_path / "pending.json",
            backup_root=tmp_path / "backups",
        )


@POSIX_SECURE_IO
def test_external_extension_rejects_foreign_component_feature_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, destination, manifest, key, proof = _external_harness(tmp_path, monkeypatch)
    component = ExtensionComponent("helper")
    adapter = _load_fixture_adapter()
    original_plan = adapter.plan

    def plan_with_foreign_feature(*args: object) -> ArtifactPlan:
        planned = original_plan(*args)
        artifact = planned.artifacts[0]
        return ArtifactPlan(
            (
                PlannedArtifact(
                    artifact.artifact,
                    artifact.planned,
                    "features.core.models",
                    artifact.reconciliation,
                ),
            ),
            planned.ownership,
        )

    monkeypatch.setattr(adapter, "plan", plan_with_foreign_feature)
    entry_point = _EntryPoint(adapter)
    monkeypatch.setattr(
        app, "discover_adapter_entry_points", lambda: {entry_point.name: entry_point}
    )

    with pytest.raises(AdapterPlanError, match="does not belong"):
        app._apply_transaction(
            str(config),
            "factory",
            True,
            {("factory", component): proof},
            journal_path=tmp_path / "pending.json",
            backup_root=tmp_path / "backups",
        )


@POSIX_SECURE_IO
def test_external_owned_artifact_cannot_change_manifest_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, destination, manifest, key, proof = _external_harness(
        tmp_path, monkeypatch, directory_write_grant=True
    )
    component = ExtensionComponent("helper")
    original = b'{"renderer":"owned"}'
    _write_text(destination, original.decode())
    prior_destination = tmp_path / "previous-rendered.json"
    _write_text(prior_destination, original.decode())
    record = ComponentOwnership(
        component,
        AdapterProvenance("io.example.helper", "example-helper-adapter"),
        "plugin-write",
        PurePosixPath("previous-rendered.json"),
        None,
        hashlib.sha256(original).hexdigest(),
        {"renderer": "fake-helper"},
    )
    manifest.write_bytes(
        ownership_manifest_bytes(OwnershipManifest(clients={"factory": ClientOwnership((record,))}))
    )
    manifest.chmod(0o600)
    journal = tmp_path / "pending.json"
    backups = tmp_path / "backups"

    with pytest.raises(app.AppError, match="identity does not match"):
        app._apply_transaction(
            str(config),
            "factory",
            True,
            {("factory", component): proof},
            journal_path=journal,
            backup_root=backups,
        )

    assert destination.read_bytes() == original
    assert prior_destination.read_bytes() == original
    assert manifest.exists()
    assert not journal.exists()
    assert not backups.exists()
