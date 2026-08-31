from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath

import pytest

from modfig import app
from modfig.adapter_routes import PathGrant
from modfig.adapters import (
    AdapterMetadata,
    AdapterPlanError,
    PreflightDeclaration,
    RuntimeProof,
)
from modfig.cli import main
from modfig.components import ExtensionComponent
from modfig.errors import AppError
from modfig.manifest import (
    AdapterProvenance,
    ClientOwnership,
    ComponentOwnership,
    OwnershipManifest,
    ownership_manifest_bytes,
)
from modfig.registry import ModelReference, RegistryValidationError, load_registry_text

POSIX_SECURE_IO = pytest.mark.skipif(os.name == "nt", reason="requires native POSIX secure I/O")

REGISTRY = """specVersion: "0.1"
providers:
  example:
    name: Example
    targets: [factory]
    baseUrl: https://api.example.com/v1
    apiKey: env.EXAMPLE_KEY
    enabled: true
    models:
      example-model:
        displayName: Example Model
        contextWindow: 8192
        maxOutputTokens: 1024
        enabled: true
"""

V01_CURSOR_ONLY = """specVersion: "0.1"
providers:
  example:
    name: Example
    targets: [cursor]
    baseUrl: https://api.example.com/v1
    apiKey: env.EXAMPLE_KEY
    enabled: true
    models:
      example-model:
        displayName: Example Model
        contextWindow: 8192
        maxOutputTokens: 1024
        enabled: true
"""

V01_CURSOR_CORE = """specVersion: "0.1"
providers:
  example:
    name: Example
    targets: [cursor]
    baseUrl: https://api.example.com/v1
    apiKey: env.EXAMPLE_KEY
    enabled: true
    models:
      example-model:
        displayName: Example Model
        contextWindow: 8192
        maxOutputTokens: 1024
        enabled: true
clientConfig:
  cursor:
    core:
      privateSchema:
        anyShape: true
"""

V01_CURSOR_AND_ZEBRA = V01_CURSOR_CORE.replace("targets: [cursor]", "targets: [cursor, zebra]")

V01_FACTORY_EXTENSION_ONLY = """specVersion: "0.1"
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
        plugin: configured
"""

V01_FACTORY_CORE_ONLY = """specVersion: "0.1"
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
"""

V01_FACTORY_SESSION_ONLY = V01_FACTORY_CORE_ONLY.replace(
    """      defaults:
        worker: {provider: router, model: primary}
        thinker: {provider: router, model: primary}
        orchestrator: {provider: router, model: primary}
        simple: {provider: router, model: primary}
        validator: {provider: router, model: primary}""",
    """      session:
        model: {provider: router, model: primary}
        reasoningEffort: high
        specModeModel: {factoryNative: factory-native-spec}""",
)

V01_NO_FACTORY = """specVersion: "0.1"
providers:
  example:
    name: Example
    targets: [vscode]
    baseUrl: https://api.example.com/v1
    apiKey: env.EXAMPLE_KEY
    enabled: true
    models:
      example-model:
        displayName: Example Model
        contextWindow: 8192
        maxOutputTokens: 1024
        enabled: true
"""

V01_VSCODE_CORE = """specVersion: "0.1"
providers:
  example:
    name: Example
    targets: [vscode]
    baseUrl: https://api.example.com/v1
    apiKey: env.EXAMPLE_KEY
    enabled: true
    models:
      example-model:
        displayName: Example Model
        contextWindow: 8192
        maxOutputTokens: 1024
        enabled: true
clientConfig:
  vscode:
    core:
      profile: work
"""

V01_CHATGPT_CORE = """specVersion: "0.1"
providers:
  example:
    name: Example
    targets: [chatgpt]
    baseUrl: https://api.example.com/v1
    apiKey: env.EXAMPLE_KEY
    provider: openai
    enabled: true
    extensions:
      chatgpt:
        default: true
    models:
      example-model:
        displayName: Example Model
        contextWindow: 8192
        maxOutputTokens: 1024
        enabled: true
clientConfig:
  chatgpt:
    core:
      profile: work
"""


def _write_registry(path: Path) -> None:
    path.write_text(REGISTRY, encoding="utf-8")
    path.chmod(0o600)


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


@POSIX_SECURE_IO
def test_diff_factory_preflights_without_runtime_probe(tmp_path: Path) -> None:
    config = tmp_path / "modfig.yaml"
    _write_text(config, V01_FACTORY_CORE_ONLY)

    app.diff(str(config), "factory")


@POSIX_SECURE_IO
def test_apply_vscode_requires_recorded_runtime_proof(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "modfig.yaml"
    _write_text(config, V01_VSCODE_CORE)

    monkeypatch.setattr(
        app.vscode,
        "discover_vscode_runtime",
        lambda: pytest.fail("public apply must not synthesize unproven runtime proof"),
    )
    monkeypatch.setenv("MODFIG_VSCODE_PROOF", str(tmp_path / "missing-proof.json"))
    assert main(["apply", "--config", str(config), "--target", "vscode", "--yes"]) == 1

    assert "runtime proof is unavailable" in capsys.readouterr().err


def test_vscode_route_selection_ignores_invalid_unselected_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", "relative")
    monkeypatch.setattr(
        app.vscode,
        "discover_vscode_user_data_root",
        lambda: tmp_path / "vscode-user",
    )

    routes = app._builtin_adapter_routes(include_chatgpt=False)

    assert routes.client_route("vscode").adapter_id == "modfig.vscode"
    with pytest.raises(app.AdapterRouteError, match="CODEX_HOME"):
        app._builtin_adapter_routes(include_chatgpt=True)


def test_public_vscode_proof_loader_uses_configured_private_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof_path = tmp_path / "proof.json"
    calls: list[Path] = []
    expected = RuntimeProof({}, "declaration")
    monkeypatch.setenv("MODFIG_VSCODE_PROOF", str(proof_path))
    monkeypatch.setattr(
        app.vscode,
        "load_vscode_runtime_proof",
        lambda path: calls.append(path) or expected,
    )

    assert app._load_public_vscode_proof() is expected
    assert calls == [proof_path.absolute()]


@POSIX_SECURE_IO
def test_diff_vscode_consumes_recorded_runtime_proof_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "modfig.yaml"
    _write_text(config, V01_VSCODE_CORE)
    user_root = tmp_path / "User"
    runtime = app.vscode.VSCodeRuntime(
        supported_os=("macos",),
        supported_channels=("stable",),
        supported_profile_modes=("default",),
        user_data_root=user_root,
        settings_path=user_root / "chatLanguageModels.json",
        state_db_path=user_root / "globalStorage" / "state.vscdb",
        state_wal_path=user_root / "globalStorage" / "state.vscdb-wal",
        state_shm_path=user_root / "globalStorage" / "state.vscdb-shm",
        safe_storage_supported=True,
        key_context="proven",
        process_quiescent=True,
        vendor_api_type_mapping=False,
        os_name="macos",
        channel="stable",
        profile_mode="default",
    )
    loaded: list[bool] = []
    monkeypatch.setenv("MODFIG_VSCODE_USER_DATA_ROOT", str(user_root))
    monkeypatch.setattr(
        app,
        "_load_public_vscode_proof",
        lambda: loaded.append(True) or RuntimeProof({}, "", provenance=runtime),
    )
    monkeypatch.setattr(
        app.vscode,
        "discover_vscode_runtime",
        lambda: pytest.fail("public diff must not synthesize unproven runtime facts"),
    )

    app.diff(str(config), "vscode")

    assert loaded == [True]


def test_vscode_transaction_snapshots_database_members_as_one_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "state.vscdb"
    connection = __import__("sqlite3").connect(db)
    connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
    connection.commit()
    connection.close()
    db.chmod(0o600)
    paths = app.vscode.DatabasePaths(
        db,
        tmp_path / "state.vscdb-wal",
        tmp_path / "state.vscdb-shm",
    )
    calls: list[app.vscode.DatabasePaths] = []
    original = app.snapshot_members

    def snapshot(bundle: app.vscode.DatabasePaths) -> dict[Path, bytes | None]:
        calls.append(bundle)
        return original(bundle)

    monkeypatch.setattr(app, "snapshot_members", snapshot)
    snapshots, versions = app._snapshot_vscode_bundle(paths)

    assert calls == [paths]
    assert snapshots[paths.database] == db.read_bytes()
    assert isinstance(snapshots[paths.wal], app.AbsentDestination)
    assert isinstance(snapshots[paths.shm], app.AbsentDestination)
    assert versions[paths.database].exists


def test_vscode_bundle_snapshot_rejects_sidecar_appearing_after_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "state.vscdb"
    connection = __import__("sqlite3").connect(db)
    connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
    connection.commit()
    connection.close()
    db.chmod(0o600)
    paths = app.vscode.DatabasePaths(
        db,
        tmp_path / "state.vscdb-wal",
        tmp_path / "state.vscdb-shm",
    )

    def snapshot(_bundle: app.vscode.DatabasePaths) -> dict[Path, bytes | None]:
        paths.wal.write_bytes(b"drift")
        paths.wal.chmod(0o600)
        return {paths.database: db.read_bytes(), paths.wal: None, paths.shm: None}

    monkeypatch.setattr(app, "snapshot_members", snapshot)
    with pytest.raises(AppError, match="changed while snapshotting"):
        app._snapshot_vscode_bundle(paths)


def test_vscode_runtime_proof_binds_provenance_before_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_root = tmp_path / "Library" / "Application Support" / "Code" / "User"
    runtime = app.vscode.VSCodeRuntime(
        supported_os=("macos",),
        supported_channels=("stable",),
        supported_profile_modes=("default",),
        user_data_root=user_root,
        settings_path=user_root / "chatLanguageModels.json",
        state_db_path=user_root / "globalStorage" / "state.vscdb",
        state_wal_path=user_root / "globalStorage" / "state.vscdb-wal",
        state_shm_path=user_root / "globalStorage" / "state.vscdb-shm",
        safe_storage_supported=True,
        key_context="proven",
        process_quiescent=True,
        vendor_api_type_mapping=False,
        os_name="macos",
        channel="stable",
        profile_mode="default",
        secret_format="oscrypt-v11",
    )
    declaration = app.vscode.adapter.preflight(app.AdapterContext("vscode", "core"))
    config_artifact = app.ArtifactIdentity(
        "vscode-config", PurePosixPath("chatLanguageModels.json")
    )
    state_artifacts = app.vscode._VSCODE_STATE_ARTIFACTS
    destinations = {
        ("vscode", "core", config_artifact): runtime.settings_path,
        ("vscode", "core", state_artifacts[0]): runtime.state_db_path,
        ("vscode", "core", state_artifacts[1]): runtime.state_wal_path,
        ("vscode", "core", state_artifacts[2]): runtime.state_shm_path,
    }
    proof = RuntimeProof({}, "", provenance=runtime)
    bound: list[object] = []
    original = app.vscode.bind_vscode_runtime_paths

    def bind(runtime: object, **kwargs: object):
        bound.append(kwargs)
        return original(runtime, **kwargs)

    monkeypatch.setattr(app.vscode, "bind_vscode_runtime_paths", bind)
    result = app._vscode_runtime_proof(declaration, destinations, proof)

    assert result.provenance is runtime
    assert result.declaration_sha256 == app.preflight_declaration_sha256(declaration)
    assert len(bound) == 1


def test_vscode_runtime_proof_rejects_missing_proof() -> None:
    declaration = app.vscode.adapter.preflight(app.AdapterContext("vscode", "core"))
    destinations = {
        ("vscode", "core", app.vscode._VSCODE_ARTIFACT): Path("/tmp/User/chatLanguageModels.json"),
        ("vscode", "core", app.vscode._VSCODE_STATE_ARTIFACTS[0]): Path(
            "/tmp/User/globalStorage/state.vscdb"
        ),
        ("vscode", "core", app.vscode._VSCODE_STATE_ARTIFACTS[1]): Path(
            "/tmp/User/globalStorage/state.vscdb-wal"
        ),
        ("vscode", "core", app.vscode._VSCODE_STATE_ARTIFACTS[2]): Path(
            "/tmp/User/globalStorage/state.vscdb-shm"
        ),
    }

    with pytest.raises(AppError, match="runtime proof is unavailable"):
        app._vscode_runtime_proof(declaration, destinations, None)


def test_target_order_and_selection_include_chatgpt() -> None:
    assert app.TARGET_ORDER == ("factory", "vscode", "chatgpt")
    assert app.selected_targets("all") == app.TARGET_ORDER
    assert app.selected_targets("chatgpt") == ("chatgpt",)
    assert tuple(app.EXPORTERS) == app.TARGET_ORDER


def test_all_target_preflight_reports_every_failure_in_order(monkeypatch) -> None:
    calls: list[str] = []
    secret = "do-not-leak-sentinel"

    class Exporter:
        def __init__(self, target: str) -> None:
            self.target = target

        def preflight(self) -> None:
            calls.append(self.target)
            raise app.AppError(f"{self.target} unavailable: {secret}")

    monkeypatch.setattr(
        app,
        "EXPORTERS",
        {target: Exporter(target) for target in app.TARGET_ORDER},
    )

    try:
        app.preflight_targets(app.selected_targets("all"))
    except app.AppError as exc:
        assert exc.message == (
            "target preflight failed: factory: unavailable; vscode: unavailable; "
            "chatgpt: unavailable"
        )
        assert secret not in exc.message
    else:
        raise AssertionError("all-target preflight unexpectedly succeeded")

    assert calls == list(app.TARGET_ORDER)


def test_factory_warning_rejects_decline(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(app.os, "isatty", lambda _fd: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    with pytest.raises(app.AppError, match="acknowledgement"):
        app._acknowledge_factory_warning(("custom:gpt-5.6-luna--openai",), False)

    assert "custom:gpt-5.6-luna--openai" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["y", "yes"])
def test_factory_warning_accepts_interactive_acknowledgement(
    answer: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(app.os, "isatty", lambda _fd: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)

    app._acknowledge_factory_warning(("custom:model--provider",), False)

    assert "custom:model--provider" in capsys.readouterr().out


def test_factory_warning_eof_rejects_acknowledgement(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(app.os, "isatty", lambda _fd: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: (_ for _ in ()).throw(EOFError))

    with pytest.raises(app.AppError, match="acknowledgement"):
        app._acknowledge_factory_warning(("custom:model--provider",), False)

    assert "custom:model--provider" in capsys.readouterr().out


def test_factory_warning_yes_prints_without_prompt(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("prompted"))

    app._acknowledge_factory_warning(("custom:gpt-5.6-luna--openai",), True)

    assert "custom:gpt-5.6-luna--openai" in capsys.readouterr().out


def test_factory_warning_non_tty_aborts_before_side_effects(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(app.os, "isatty", lambda _fd: False)
    with pytest.raises(app.AppError, match="acknowledgement"):
        app._acknowledge_factory_warning(("custom:model--provider",), False)
    assert "custom:model--provider" in capsys.readouterr().out


@POSIX_SECURE_IO
def test_factory_warning_eof_aborts_transaction_before_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, settings, manifest = _factory_transaction(tmp_path, monkeypatch)
    _seed_owned_factory_model(settings, manifest)
    before = settings.read_bytes()
    manifest_before = manifest.read_bytes()
    journal = tmp_path / "pending.json"
    backups = tmp_path / "backups"
    monkeypatch.setattr(app.os, "isatty", lambda _fd: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: (_ for _ in ()).throw(EOFError))
    monkeypatch.setattr(app, "create_backup_set", lambda *args, **kwargs: pytest.fail("backup"))
    monkeypatch.setattr(app, "save_journal", lambda *args, **kwargs: pytest.fail("journal"))

    with pytest.raises(app.AppError, match="acknowledgement"):
        app._apply_transaction(
            str(config), "factory", False, journal_path=journal, backup_root=backups
        )

    assert settings.read_bytes() == before
    assert not journal.exists()
    assert not backups.exists()
    assert manifest.read_bytes() == manifest_before


@POSIX_SECURE_IO
def test_factory_additions_only_apply_does_not_warn_or_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, settings, manifest = _factory_transaction(tmp_path, monkeypatch)
    monkeypatch.setattr(app.os, "isatty", lambda _fd: False)
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("prompted"))

    app._apply_transaction(
        str(config),
        "factory",
        False,
        journal_path=tmp_path / "pending.json",
        backup_root=tmp_path / "backups",
    )

    assert b"custom:example-model--example" in settings.read_bytes()
    assert manifest.exists()


@POSIX_SECURE_IO
def test_factory_warning_happens_after_planning_before_backup_and_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, settings, manifest = _factory_transaction(tmp_path, monkeypatch)
    _seed_owned_factory_model(settings, manifest)
    events: list[str] = []
    adapter_type = type(app.factory.adapter)
    original_preflight = adapter_type.preflight
    original_plan = adapter_type.plan
    original_backup = app.create_backup_set
    original_save_journal = app.save_journal

    def preflight(self, context):
        events.append("preflight")
        return original_preflight(self, context)

    def plan(self, context, snapshots, ownership):
        events.append("plan")
        return original_plan(self, context, snapshots, ownership)

    def acknowledge(*args, **kwargs):
        events.append("warning")

    def create_backup(*args, **kwargs):
        events.append("backup")
        return original_backup(*args, **kwargs)

    def save(*args, **kwargs):
        events.append("journal")
        return original_save_journal(*args, **kwargs)

    monkeypatch.setattr(adapter_type, "preflight", preflight)
    monkeypatch.setattr(adapter_type, "plan", plan)
    monkeypatch.setattr(app, "_acknowledge_factory_warning", acknowledge)
    monkeypatch.setattr(app, "create_backup_set", create_backup)
    monkeypatch.setattr(app, "save_journal", save)

    app._apply_transaction(
        str(config),
        "factory",
        False,
        journal_path=tmp_path / "pending.json",
        backup_root=tmp_path / "backups",
    )

    assert events.index("preflight") < events.index("plan") < events.index("warning")
    assert events.index("warning") < events.index("backup") < events.index("journal")


def _seed_owned_factory_model(settings: Path, manifest: Path) -> None:
    _write_text(
        settings,
        json.dumps(
            {
                "customModels": [
                    {
                        "id": "custom:example-model--example",
                        "model": "example-model",
                        "displayName": "old",
                    }
                ],
                "modelFavorites": [],
            }
        ),
    )
    digest = hashlib.sha256(settings.read_bytes()).hexdigest()
    record = ComponentOwnership(
        "core",
        AdapterProvenance("modfig.factory", "modfig"),
        "factory-config",
        PurePosixPath("settings.json"),
        digest,
        digest,
        {"modelIds": ["custom:example-model--example"], "favoriteIds": [], "fields": []},
    )
    manifest.write_bytes(
        ownership_manifest_bytes(OwnershipManifest(clients={"factory": ClientOwnership((record,))}))
    )
    manifest.chmod(0o600)


def test_non_factory_apply_requires_confirmation_before_transaction_preparation(
    monkeypatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(app, "load_valid_registry", lambda config: calls.append("registry"))

    with pytest.raises(app.AppError, match="--yes"):
        app.apply("registry.yaml", "chatgpt", yes=False)

    assert calls == []


def test_factory_noop_skips_transaction_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, settings, manifest = _factory_transaction(tmp_path, monkeypatch)
    app._apply_transaction(
        str(config),
        "factory",
        True,
        journal_path=tmp_path / "first.json",
        backup_root=tmp_path / "first-backups",
    )
    before = settings.read_bytes()
    app._apply_transaction(
        str(config),
        "factory",
        True,
        journal_path=tmp_path / "pending.json",
        backup_root=tmp_path / "backups",
    )
    assert settings.read_bytes() == before
    assert manifest.exists()

    calls: list[str] = []

    monkeypatch.setattr(
        app,
        "_merged_adapter_routes",
        lambda **kwargs: calls.append("routes") or app.AdapterRoutes(),
    )
    with pytest.raises(app.AppError, match="cannot read registry"):
        app.apply("registry.yaml", "factory", yes=True)

    assert calls == []


def test_pending_recovery_runs_before_registry_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    journal = tmp_path / "pending.json"
    journal.write_text("pending")
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(app, "resolve_manifest_path", lambda *_: manifest)

    def recover(*args: object, **kwargs: object) -> None:
        del args, kwargs
        calls.append("recover")
        raise app.AppError("stop after recovery")

    monkeypatch.setattr(app, "_recover_pending", recover)
    monkeypatch.setattr(app, "load_valid_registry", lambda *_: calls.append("registry"))
    monkeypatch.setattr(app, "_merged_adapter_routes", lambda **kwargs: calls.append("routes"))

    with pytest.raises(app.AppError, match="stop after recovery"):
        app._apply_transaction(
            "registry.yaml",
            "factory",
            True,
            {},
            journal_path=journal,
            backup_root=tmp_path / "backups",
        )

    assert calls == ["recover"]


def test_file_grant_rejects_noncanonical_relative_path(tmp_path: Path) -> None:
    granted = tmp_path / "settings.json"
    route = app.AdapterRoute(
        "factory",
        "core",
        "modfig.factory",
        "modfig",
        True,
        (),
        (PathGrant("factory-config", "file", granted, None),),
        True,
    )

    with pytest.raises(app.AppError, match="file grant"):
        app._grant_destination(
            route,
            app.ArtifactIdentity("factory-config", PurePosixPath("nested/settings.json")),
            write=True,
        )


def test_duplicate_destination_is_rejected() -> None:
    destination = Path("/tmp/settings.json")
    with pytest.raises(app.AppError, match="duplicate|overlap"):
        app._require_unique_destinations((destination, destination))


def _factory_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    config = tmp_path / "modfig.yaml"
    _write_registry(config)
    settings = tmp_path / "settings.json"
    _write_text(settings, '{"customModels":[],"modelFavorites":[]}')
    manifest = tmp_path / "manifest.json"
    grant = PathGrant("factory-config", "file", settings, None)
    route = app.AdapterRoute(
        "factory", "core", "modfig.factory", "modfig", True, (grant,), (grant,), True
    )
    monkeypatch.setattr(app, "resolve_manifest_path", lambda *_: manifest)
    monkeypatch.setattr(app, "_merged_adapter_routes", lambda **kwargs: app.AdapterRoutes((route,)))
    # ponytail: transaction tests run after probe behavior covered in test_probe.py.
    monkeypatch.setattr(app.factory, "probe_factory_models", lambda *args, **kwargs: None)
    return config, settings, manifest


def test_public_factory_apply_uses_shared_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, settings, manifest = _factory_transaction(tmp_path, monkeypatch)
    calls: list[tuple[str, object]] = []
    transaction = app._apply_transaction

    def apply_through_transaction(*args: object, **kwargs: object) -> None:
        calls.append((args[1], args[3]))
        transaction(*args, **kwargs)

    monkeypatch.setattr(app, "_apply_transaction", apply_through_transaction)

    app.apply(str(config), "factory", yes=True)

    assert calls == [("factory", {})]
    assert b"custom:example-model--example" in settings.read_bytes()
    assert manifest.exists()


@POSIX_SECURE_IO
def test_factory_transaction_ignores_injected_runtime_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, settings, _manifest = _factory_transaction(tmp_path, monkeypatch)
    fake_proof = RuntimeProof({"untrusted": "proof"}, "mismatched-declaration")

    app._apply_transaction(
        str(config),
        "factory",
        True,
        {("factory", "core"): fake_proof},
        journal_path=tmp_path / "pending.json",
        backup_root=tmp_path / "backups",
    )

    assert b"custom:example-model--example" in settings.read_bytes()


@POSIX_SECURE_IO
def test_factory_transaction_writes_manifest_last_and_cleans_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, settings, manifest = _factory_transaction(tmp_path, monkeypatch)
    journal = tmp_path / "pending.json"
    backups = tmp_path / "backups"
    events: list[str] = []
    original_write = app.conditional_write_bytes

    def write(*args: object, **kwargs: object):
        path = args[0]
        events.append("manifest" if path == manifest else "destination")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(app, "conditional_write_bytes", write)

    app._apply_transaction(
        str(config),
        "factory",
        True,
        journal_path=journal,
        backup_root=backups,
    )

    output = settings.read_bytes()
    assert b"custom:example-model--example" in output
    assert events[-1] == "manifest"
    record = app.load_ownership_manifest(manifest).clients["factory"].components[0]
    assert record.written_sha256 == hashlib.sha256(output).hexdigest()
    assert not journal.exists()
    assert not backups.exists() or list(backups.iterdir()) == []


@POSIX_SECURE_IO
def test_host_rejects_postwrite_bytes_that_do_not_match_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, settings, manifest = _factory_transaction(tmp_path, monkeypatch)
    journal = tmp_path / "pending.json"
    backups = tmp_path / "backups"
    before = settings.read_bytes()
    original_write = app.conditional_write_bytes
    skipped = False

    def drop_first_destination_write(*args: object, **kwargs: object):
        nonlocal skipped
        path, content, expected = args[:3]
        if path == settings and content != before and not skipped:
            skipped = True
            return expected
        return original_write(*args, **kwargs)

    monkeypatch.setattr(app, "conditional_write_bytes", drop_first_destination_write)

    with pytest.raises(app.AppError, match="post-write state does not match planned artifact"):
        app._apply_transaction(
            str(config),
            "factory",
            True,
            journal_path=journal,
            backup_root=backups,
        )

    assert skipped
    assert settings.read_bytes() == before
    assert not manifest.exists()
    assert not journal.exists()
    assert not backups.exists() or list(backups.iterdir()) == []


@POSIX_SECURE_IO
def test_factory_defaults_plan_records_field_ownership_without_runtime_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, settings, manifest = _factory_transaction(tmp_path, monkeypatch)
    _write_text(config, V01_FACTORY_CORE_ONLY)

    app._apply_transaction(
        str(config),
        "factory",
        True,
        journal_path=tmp_path / "pending.json",
        backup_root=tmp_path / "backups",
    )

    output = json.loads(settings.read_text(encoding="utf-8"))
    assert output["agents"]["worker"]["model"] == "custom:primary--router"
    record = app.load_ownership_manifest(manifest).clients["factory"].components[0]
    assert len(record.ownership["fields"]) == 5


@POSIX_SECURE_IO
def test_owned_factory_defaults_pointer_mismatch_fails_during_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, settings, manifest = _factory_transaction(tmp_path, monkeypatch)
    _write_text(config, V01_FACTORY_CORE_ONLY)
    record = ComponentOwnership(
        "core",
        AdapterProvenance("modfig.factory", "modfig"),
        "factory-config",
        PurePosixPath("settings.json"),
        hashlib.sha256(settings.read_bytes()).hexdigest(),
        hashlib.sha256(settings.read_bytes()).hexdigest(),
        {
            "modelIds": [],
            "favoriteIds": [],
            "fields": [
                {
                    "logicalKey": "defaults.worker",
                    "jsonPointer": "/stale/worker/model",
                    "before": {"kind": "absent"},
                    "writtenSha256": "0" * 64,
                }
            ],
        },
    )
    manifest.write_bytes(
        ownership_manifest_bytes(OwnershipManifest(clients={"factory": ClientOwnership((record,))}))
    )
    manifest.chmod(0o600)

    with pytest.raises(AdapterPlanError, match="pointer disagrees"):
        app._apply_transaction(
            str(config),
            "factory",
            True,
            journal_path=tmp_path / "pending.json",
            backup_root=tmp_path / "backups",
        )


@POSIX_SECURE_IO
def test_factory_defaults_transaction_rolls_back_full_settings_after_late_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, settings, manifest = _factory_transaction(tmp_path, monkeypatch)
    _write_text(config, V01_FACTORY_CORE_ONLY)
    before = settings.read_bytes()
    original_write = app.conditional_write_bytes

    def fail_manifest(*args: object, **kwargs: object):
        if args[0] == manifest:
            raise app.AppError("late manifest failure")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(app, "conditional_write_bytes", fail_manifest)
    with pytest.raises(app.AppError, match="late manifest failure"):
        app._apply_transaction(
            str(config),
            "factory",
            True,
            journal_path=tmp_path / "pending.json",
            backup_root=tmp_path / "backups",
        )
    assert settings.read_bytes() == before


@POSIX_SECURE_IO
def test_transaction_rollback_preserves_concurrent_client_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, settings, manifest = _factory_transaction(tmp_path, monkeypatch)
    _write_text(config, V01_FACTORY_CORE_ONLY)
    journal = tmp_path / "pending.json"
    backups = tmp_path / "backups"
    original_write = app.conditional_write_bytes

    def fail_manifest_after_concurrent_edit(*args: object, **kwargs: object):
        if args[0] == manifest:
            _write_text(settings, '{"manual":"edit"}')
            raise app.AppError("late manifest failure")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(app, "conditional_write_bytes", fail_manifest_after_concurrent_edit)

    with pytest.raises(app.AppError, match="rollback is incomplete; recovery required"):
        app._apply_transaction(
            str(config),
            "factory",
            True,
            journal_path=journal,
            backup_root=backups,
        )

    assert settings.read_bytes() == b'{"manual":"edit"}'
    assert journal.exists()
    assert list(backups.iterdir())


@POSIX_SECURE_IO
def test_manifest_save_failure_rolls_back_destination_and_cleans_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, settings, manifest = _factory_transaction(tmp_path, monkeypatch)
    journal = tmp_path / "pending.json"
    backups = tmp_path / "backups"
    before = settings.read_bytes()
    original_write = app.conditional_write_bytes

    def fail_manifest(*args: object, **kwargs: object):
        if args[0] == manifest:
            raise app.AppError("manifest save failed")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(app, "conditional_write_bytes", fail_manifest)

    with pytest.raises(app.AppError, match="manifest save failed"):
        app._apply_transaction(
            str(config),
            "factory",
            True,
            journal_path=journal,
            backup_root=backups,
        )

    assert settings.read_bytes() == before
    assert not manifest.exists()
    assert not journal.exists()
    assert not backups.exists() or list(backups.iterdir()) == []


@POSIX_SECURE_IO
def test_manifest_provenance_mismatch_fails_before_adapter_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, settings, manifest = _factory_transaction(tmp_path, monkeypatch)
    record = ComponentOwnership(
        "core",
        AdapterProvenance("other.adapter", "other-distribution"),
        "factory-config",
        PurePosixPath("settings.json"),
        None,
        hashlib.sha256(settings.read_bytes()).hexdigest(),
        {},
    )
    manifest.write_bytes(
        ownership_manifest_bytes(OwnershipManifest(clients={"factory": ClientOwnership((record,))}))
    )
    imported: list[str] = []
    monkeypatch.setattr(
        app,
        "_adapter_for_route",
        lambda *_: imported.append("import") or app.factory.adapter,
    )

    with pytest.raises(app.AppError, match="provenance"):
        app._apply_transaction(
            str(config),
            "factory",
            True,
            journal_path=tmp_path / "pending.json",
            backup_root=tmp_path / "backups",
        )

    assert imported == []


def test_explicit_chatgpt_apply_fails_before_registry_side_effects(
    tmp_path: Path, monkeypatch
) -> None:
    config = tmp_path / "modfig.yaml"
    _write_text(config, V01_CHATGPT_CORE)
    calls: list[str] = []
    monkeypatch.setenv("MODFIG_CHATGPT_PROOF", str(tmp_path / "missing-proof.json"))

    with pytest.raises(app.AppError, match="ChatGPT runtime proof is unavailable"):
        app.apply(str(config), "chatgpt", yes=True)

    assert calls == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["modfig.yaml"]


def test_factory_plan_context_contains_only_factory_emitted_model_dtos() -> None:
    registry = load_registry_text(REGISTRY)

    context = app.adapter_plan_context("factory", "core", registry)

    assert tuple((model.provider_key, model.model) for model in context.models) == (
        ("example", "example-model"),
    )
    assert context.resolve_model(ModelReference("example", "example-model")) == context.models[0]
    assert all(
        cell.cell_contents is not registry for cell in context._resolve_model.__closure__ or ()
    )
    with pytest.raises(RegistryValidationError, match="does not target"):
        app.adapter_plan_context("vscode", "core", registry).resolve_model(
            ModelReference("example", "example-model")
        )


def test_provider_only_factory_registry_selects_core_model_writer() -> None:
    registry = load_registry_text(REGISTRY)

    assert app.selected_components("factory", registry, {}) == ("core",)


def test_disabled_provider_target_does_not_select_core_model_writer() -> None:
    registry = load_registry_text(REGISTRY.replace("enabled: true", "enabled: false", 1))

    assert app.selected_components("factory", registry, {}) == ()


def test_all_selects_only_declared_client_in_canonical_order() -> None:
    registry = load_registry_text(V01_CURSOR_ONLY)
    assert app.selected_clients("all", registry, ()) == ("cursor",)


@POSIX_SECURE_IO
def test_declared_external_client_without_core_still_requires_primary_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "modfig.yaml"
    _write_text(config, V01_CURSOR_ONLY)
    monkeypatch.setattr(app, "load_owned_components", lambda *a, **k: {})
    monkeypatch.setattr(
        "modfig.app.resolve_adapter_routes_path", lambda *_: tmp_path / "absent-routes.yaml"
    )
    monkeypatch.setattr("modfig.app.Path.home", lambda: tmp_path)

    discover_calls: list[str] = []
    monkeypatch.setattr(
        app, "discover_adapter_entry_points", lambda: discover_calls.append("discover") or {}
    )

    with pytest.raises(app.AppError, match="no adapter route"):
        app.diff(str(config), "all")

    assert discover_calls == []


def test_all_selects_configured_client_without_provider_target() -> None:
    registry = load_registry_text(V01_FACTORY_CORE_ONLY)
    assert app.selected_clients("all", registry, ()) == ("factory",)


def test_all_selects_manifest_only_client_for_owned_state_reconciliation() -> None:
    registry = load_registry_text(V01_NO_FACTORY)
    assert app.selected_clients("all", registry, {"factory": {"core"}}) == ("factory", "vscode")


def test_all_includes_manifest_only_client_alongside_declared() -> None:
    registry = load_registry_text(V01_NO_FACTORY)
    assert "factory" in app.selected_clients("all", registry, {"factory": {"core"}})


def test_selects_manifest_only_extension_for_cleanup() -> None:
    registry = load_registry_text(V01_FACTORY_CORE_ONLY)
    owned = {"factory": {ExtensionComponent("oh-my-droid")}}
    assert app.selected_components("factory", registry, owned) == (
        "core",
        ExtensionComponent("oh-my-droid"),
    )


def test_explicit_client_is_selected_even_without_desired_state() -> None:
    registry = load_registry_text(V01_CURSOR_ONLY)
    assert app.selected_clients("factory", registry, ()) == ("factory",)


def test_all_orders_builtins_first_then_third_parties_lexically() -> None:
    text = """specVersion: "0.1"
providers:
  example:
    name: Example
    targets: [cursor, factory, zebra, chatgpt]
    baseUrl: https://api.example.com/v1
    apiKey: env.EXAMPLE_KEY
    provider: openai
    enabled: true
    extensions:
      chatgpt:
        default: true
    models:
      example-model:
        displayName: Example Model
        contextWindow: 8192
        maxOutputTokens: 1024
        enabled: true
"""
    registry = load_registry_text(text)
    assert app.selected_clients("all", registry, ()) == ("factory", "chatgpt", "cursor", "zebra")


def test_selected_components_orders_core_first_then_extensions_lexically() -> None:
    text = """specVersion: "0.1"
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
      zeta-extension: {sourcePlugin: zeta@zeta}
      alpha-extension: {sourcePlugin: alpha@alpha}
"""
    registry = load_registry_text(text)
    owned = {"factory": {ExtensionComponent("mid-extension"), "core"}}
    assert app.selected_components("factory", registry, owned) == (
        "core",
        ExtensionComponent("alpha-extension"),
        ExtensionComponent("mid-extension"),
        ExtensionComponent("zeta-extension"),
    )


class _Dist:
    def __init__(self, name: str, entry_points: list[object]) -> None:
        self.name = name
        self.entry_points = entry_points


class _EntryPoint:
    def __init__(self, name: str, distribution: str, adapter: object) -> None:
        self.name = name
        self.group = "modfig.adapters.v1"
        self.value = "example_cursor:adapter"
        self.dist = _Dist(distribution, [self])
        self._adapter = adapter
        self.loaded = 0

    def load(self) -> object:
        self.loaded += 1
        return self._adapter


class _RecordingCursorAdapter:
    def __init__(self) -> None:
        self.preflighted: list[tuple[str, str]] = []

    def describe(self) -> AdapterMetadata:
        return AdapterMetadata("io.example.cursor", "cursor", "core")

    def validate(self, config: object, context: object) -> None:
        del config, context

    def preflight(self, context: object) -> PreflightDeclaration:
        assert isinstance(context, app.AdapterContext)
        self.preflighted.append((context.logical_client, "core"))
        return PreflightDeclaration({}, (), ())

    def plan(self, *args: object) -> object:
        raise AssertionError("plan must not run in Task 4 selection/preflight")

    def recheck(self, proof: object) -> None:
        raise AssertionError("recheck must not run in Task 4")

    def verify(self, *args: object) -> None:
        raise AssertionError("verify must not run in Task 4")


class _RecordingFactoryExtensionAdapter:
    def __init__(self, metadata: AdapterMetadata) -> None:
        self.metadata = metadata
        self.preflighted: list[tuple[str, ExtensionComponent]] = []

    def describe(self) -> AdapterMetadata:
        return self.metadata

    def validate(self, config: object, context: object) -> None:
        del config, context

    def preflight(self, context: object) -> PreflightDeclaration:
        assert isinstance(context, app.AdapterContext)
        assert isinstance(context.component, ExtensionComponent)
        self.preflighted.append((context.logical_client, context.component))
        return PreflightDeclaration({}, (), ())

    def plan(self, *args: object) -> object:
        raise AssertionError("plan must not run in Task 4 selection/preflight")

    def recheck(self, proof: object) -> None:
        raise AssertionError("recheck must not run in Task 4")

    def verify(self, *args: object) -> None:
        raise AssertionError("verify must not run in Task 4")


def _write_factory_extension_routes(path: Path, extension_root: Path) -> None:
    extension_root.mkdir(exist_ok=True)
    path.write_text(
        f"""adapterConfigVersion: "1"
extensions:
  factory:
    helper:
      adapter: io.example.factory.helper
      distribution: example-helper
      enabled: true
      readGrants: []
      writeGrants:
        - id: droid-settings
          kind: file
          root: {extension_root / "settings.json"}
""",
        encoding="utf-8",
    )
    path.chmod(0o600)


@POSIX_SECURE_IO
def test_factory_extension_without_route_fails_before_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "modfig.yaml"
    _write_text(config, V01_FACTORY_EXTENSION_ONLY)
    monkeypatch.setattr(app, "load_owned_components", lambda *a, **k: {})
    monkeypatch.setattr(
        "modfig.app.resolve_adapter_routes_path", lambda *_: tmp_path / "absent-routes.yaml"
    )
    monkeypatch.setattr("modfig.app.Path.home", lambda: tmp_path)

    discover_calls: list[str] = []
    monkeypatch.setattr(
        app, "discover_adapter_entry_points", lambda: discover_calls.append("discover") or {}
    )

    with pytest.raises(app.AppError, match="no adapter route"):
        app.diff(str(config), "factory")

    assert discover_calls == []


@POSIX_SECURE_IO
def test_factory_extension_imports_and_preflights_bound_extension_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "modfig.yaml"
    _write_text(config, V01_FACTORY_EXTENSION_ONLY)
    routes_path = tmp_path / "adapters.yaml"
    _write_factory_extension_routes(routes_path, tmp_path / "droids")
    monkeypatch.setattr(app, "load_owned_components", lambda *a, **k: {})
    monkeypatch.setattr("modfig.app.resolve_adapter_routes_path", lambda *_: routes_path)
    monkeypatch.setattr("modfig.app.Path.home", lambda: tmp_path)

    component = ExtensionComponent("helper")
    adapter = _RecordingFactoryExtensionAdapter(
        AdapterMetadata("io.example.factory.helper", "factory", component)
    )
    extension_ep = _EntryPoint("io.example.factory.helper", "example-helper", adapter)
    monkeypatch.setattr(
        app, "discover_adapter_entry_points", lambda: {extension_ep.name: extension_ep}
    )

    class _FactoryExporter:
        def preflight(self) -> None:
            raise AssertionError("Factory core must not be preflighted without a core component")

    monkeypatch.setattr(app, "EXPORTERS", {"factory": _FactoryExporter()})

    app.diff(str(config), "factory")

    assert extension_ep.loaded == 1
    assert adapter.preflighted == [("factory", component)]


@POSIX_SECURE_IO
def test_factory_extension_metadata_mismatch_fails_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "modfig.yaml"
    _write_text(config, V01_FACTORY_EXTENSION_ONLY)
    routes_path = tmp_path / "adapters.yaml"
    _write_factory_extension_routes(routes_path, tmp_path / "droids")
    monkeypatch.setattr(app, "load_owned_components", lambda *a, **k: {})
    monkeypatch.setattr("modfig.app.resolve_adapter_routes_path", lambda *_: routes_path)
    monkeypatch.setattr("modfig.app.Path.home", lambda: tmp_path)

    adapter = _RecordingFactoryExtensionAdapter(
        AdapterMetadata("io.example.factory.helper", "factory", ExtensionComponent("other"))
    )
    extension_ep = _EntryPoint("io.example.factory.helper", "example-helper", adapter)
    monkeypatch.setattr(
        app, "discover_adapter_entry_points", lambda: {extension_ep.name: extension_ep}
    )

    with pytest.raises(app.AdapterRouteError, match="metadata"):
        app.diff(str(config), "factory")

    assert extension_ep.loaded == 1


def _write_cursor_routes(path: Path, cursor_root: Path, *, enabled: bool = True) -> None:
    cursor_root.mkdir(exist_ok=True)
    path.write_text(
        f"""adapterConfigVersion: "1"
clients:
  cursor:
    adapter: io.example.cursor
    distribution: example-cursor
    enabled: {str(enabled).lower()}
    readGrants: []
    writeGrants:
      - id: cursor-settings
        kind: file
        root: {cursor_root / "settings.json"}
""",
        encoding="utf-8",
    )
    path.chmod(0o600)


@POSIX_SECURE_IO
def test_all_target_imports_and_preflights_only_selected_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "modfig.yaml"
    _write_text(config, V01_CURSOR_CORE)
    routes_path = tmp_path / "adapters.yaml"
    _write_cursor_routes(routes_path, tmp_path / "cursor")
    monkeypatch.setattr("modfig.app.resolve_adapter_routes_path", lambda *_: routes_path)
    monkeypatch.setattr("modfig.app.Path.home", lambda: tmp_path)
    monkeypatch.setattr(app, "load_owned_components", lambda *a, **k: {})

    cursor_adapter = _RecordingCursorAdapter()
    cursor_ep = _EntryPoint("io.example.cursor", "example-cursor", cursor_adapter)
    monkeypatch.setattr(
        app, "discover_adapter_entry_points", lambda: {"io.example.cursor": cursor_ep}
    )

    builtin_calls: list[str] = []

    class _FailingExporter:
        def preflight(self) -> None:
            builtin_calls.append("called")
            raise AssertionError("builtin must not be preflighted for cursor-only registry")

    monkeypatch.setattr(app, "EXPORTERS", {name: _FailingExporter() for name in app.TARGET_ORDER})

    app.diff(str(config), "all")

    assert cursor_ep.loaded == 1
    assert cursor_adapter.preflighted == [("cursor", "core")]
    assert builtin_calls == []
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "adapters.yaml",
        "cursor",
        "modfig.yaml",
    ]


@POSIX_SECURE_IO
def test_all_resolves_later_client_routes_before_importing_earlier_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "modfig.yaml"
    _write_text(config, V01_CURSOR_AND_ZEBRA)
    routes_path = tmp_path / "adapters.yaml"
    _write_cursor_routes(routes_path, tmp_path / "cursor")
    monkeypatch.setattr(app, "load_owned_components", lambda *a, **k: {})
    monkeypatch.setattr("modfig.app.resolve_adapter_routes_path", lambda *_: routes_path)
    monkeypatch.setattr("modfig.app.Path.home", lambda: tmp_path)

    cursor_adapter = _RecordingCursorAdapter()
    cursor_ep = _EntryPoint("io.example.cursor", "example-cursor", cursor_adapter)
    monkeypatch.setattr(app, "discover_adapter_entry_points", lambda: {cursor_ep.name: cursor_ep})

    with pytest.raises(app.AppError, match="zebra: unavailable"):
        app.diff(str(config), "all")

    assert cursor_ep.loaded == 0
    assert cursor_adapter.preflighted == []


@POSIX_SECURE_IO
def test_manifest_owned_client_without_route_fails_without_import_or_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "modfig.yaml"
    _write_text(config, V01_FACTORY_CORE_ONLY)
    monkeypatch.setattr(app, "load_owned_components", lambda *a, **k: {"cursor": ("core",)})
    monkeypatch.setattr(
        "modfig.app.resolve_adapter_routes_path", lambda *_: tmp_path / "absent-routes.yaml"
    )
    monkeypatch.setattr("modfig.app.Path.home", lambda: tmp_path)

    discover_calls: list[str] = []
    monkeypatch.setattr(
        app, "discover_adapter_entry_points", lambda: discover_calls.append("discover") or {}
    )

    with pytest.raises(app.AppError, match="no adapter route"):
        app.diff(str(config), "cursor")

    assert discover_calls == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["modfig.yaml"]


@POSIX_SECURE_IO
def test_manifest_owned_client_with_disabled_route_fails_without_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "modfig.yaml"
    _write_text(config, V01_FACTORY_CORE_ONLY)
    routes_path = tmp_path / "adapters.yaml"
    _write_cursor_routes(routes_path, tmp_path / "cursor", enabled=False)
    monkeypatch.setattr(app, "load_owned_components", lambda *a, **k: {"cursor": ("core",)})
    monkeypatch.setattr("modfig.app.resolve_adapter_routes_path", lambda *_: routes_path)
    monkeypatch.setattr("modfig.app.Path.home", lambda: tmp_path)

    cursor_ep = _EntryPoint("io.example.cursor", "example-cursor", _RecordingCursorAdapter())
    monkeypatch.setattr(
        app, "discover_adapter_entry_points", lambda: {"io.example.cursor": cursor_ep}
    )

    with pytest.raises(app.AppError, match="disabled"):
        app.diff(str(config), "cursor")

    assert cursor_ep.loaded == 0


_SHA = "a" * 64


def _write_v3_manifest(path: Path) -> None:
    manifest = OwnershipManifest(
        clients={
            "factory": ClientOwnership(
                components=(
                    ComponentOwnership(
                        component="core",
                        adapter=AdapterProvenance("io.modfig.factory", "builtin"),
                        grant_id="factory-settings",
                        artifact_path=PurePosixPath("settings.json"),
                        preimage_sha256=None,
                        written_sha256=_SHA,
                        ownership={},
                    ),
                    ComponentOwnership(
                        component=ExtensionComponent("oh-my-droid"),
                        adapter=AdapterProvenance(
                            "io.example.factory.oh-my-droid", "example-oh-my-droid"
                        ),
                        grant_id="droid-settings",
                        artifact_path=PurePosixPath("droids/settings.json"),
                        preimage_sha256=None,
                        written_sha256=_SHA,
                        ownership={},
                    ),
                ),
            ),
            "cursor": ClientOwnership(
                components=(
                    ComponentOwnership(
                        component="core",
                        adapter=AdapterProvenance("io.example.cursor", "example-cursor"),
                        grant_id="cursor-settings",
                        artifact_path=PurePosixPath("settings.json"),
                        preimage_sha256=None,
                        written_sha256=_SHA,
                        ownership={},
                    ),
                ),
            ),
        }
    )
    path.write_bytes(ownership_manifest_bytes(manifest))
    path.chmod(0o600)


@POSIX_SECURE_IO
def test_load_owned_components_returns_v3_components_for_existing_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_v3_manifest(manifest_path)
    monkeypatch.setattr(app, "resolve_manifest_path", lambda *_: manifest_path)

    owned = app.load_owned_components()

    assert owned["factory"] == ("core", ExtensionComponent("oh-my-droid"))
    assert owned["cursor"] == ("core",)


@POSIX_SECURE_IO
def test_load_owned_components_empty_for_missing_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "manifest.json"
    monkeypatch.setattr(app, "resolve_manifest_path", lambda *_: manifest_path)

    assert dict(app.load_owned_components()) == {}


@POSIX_SECURE_IO
def test_load_owned_components_empty_for_missing_manifest_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "nested" / "missing" / "manifest.json"
    monkeypatch.setattr(app, "resolve_manifest_path", lambda *_: manifest_path)

    assert dict(app.load_owned_components()) == {}
    assert not (tmp_path / "nested").exists()


@POSIX_SECURE_IO
def test_load_owned_components_propagates_invalid_manifest_without_swallowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_text(manifest_path, '{"manifestVersion": 2, "targets": {}}\n')
    monkeypatch.setattr(app, "resolve_manifest_path", lambda *_: manifest_path)

    with pytest.raises(app.AppError, match="current manifest v3"):
        app.load_owned_components()
