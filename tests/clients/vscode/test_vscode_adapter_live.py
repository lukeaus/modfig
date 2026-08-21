from __future__ import annotations

import sqlite3
from pathlib import Path, PurePosixPath

import pytest

from modfig.adapters import (
    AbsentDestination,
    AdapterContext,
    AdapterPlanContext,
    ArtifactIdentity,
    ResolvedModel,
    RuntimeProof,
)
from modfig.clients.vscode import (
    _VSCODE_ARTIFACT,
    _VSCODE_STATE_ARTIFACTS,
    VSCodeRuntime,
    adapter,
    preflight,
)
from modfig.errors import AppError


def _runtime(root: Path, *, os_name: str = "linux", channel: str = "stable") -> VSCodeRuntime:
    return VSCodeRuntime(
        supported_os=("macos", "linux"),
        supported_channels=("stable",),
        supported_profile_modes=("default",),
        user_data_root=root,
        settings_path=root / "chatLanguageModels.json",
        state_db_path=root / "globalStorage" / "state.vscdb",
        state_wal_path=root / "globalStorage" / "state.vscdb-wal",
        state_shm_path=root / "globalStorage" / "state.vscdb-shm",
        safe_storage_supported=True,
        key_context="proofed",
        process_quiescent=True,
        vendor_api_type_mapping=False,
        runtime_recheck=lambda: True,
        os_name=os_name,
        channel=channel,
        profile_mode="default",
        secret_format="oscrypt-v11" if os_name == "linux" else "oscrypt-v10",
    )


def test_vscode_adapter_plans_settings_and_database_members(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    settings = b'{"providers": []}\n'
    proof = RuntimeProof(
        {"channel": "stable", "platform": "linux"},
        "declaration",
        provenance=runtime,
    )

    plan = adapter.plan(
        AdapterPlanContext("vscode", "core", {}, models=()),
        proof,
        {
            _VSCODE_ARTIFACT: settings,
            ArtifactIdentity("vscode-state", PurePosixPath("state.vscdb")): b"database",
            ArtifactIdentity("vscode-state", PurePosixPath("state.vscdb-wal")): b"wal",
            ArtifactIdentity("vscode-state", PurePosixPath("state.vscdb-shm")): b"shm",
        },
        {},
    )

    assert len(plan.artifacts) == 4
    assert plan.artifacts[0].planned == b'{\n  "providers": []\n}\n'
    assert [str(item.artifact.relative_path) for item in plan.artifacts[1:]] == [
        "state.vscdb",
        "state.vscdb-wal",
        "state.vscdb-shm",
    ]


def test_vscode_adapter_plans_owned_secret_row_without_foreign_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from modfig.adapters import ResolvedModel
    from modfig.clients.vscode import _VSCODE_STATE_ARTIFACTS

    runtime = _runtime(tmp_path)

    object.__setattr__(runtime, "secret_backend", lambda: b"secret-service-value")
    object.__setattr__(runtime, "secret_values", {"ROUTER_KEY": b"secret-value"})
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
    )
    monkeypatch.setenv("ROUTER_KEY", "secret-value")
    db = tmp_path / "state.vscdb"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
    connection.execute("INSERT INTO ItemTable VALUES (?, ?)", ("foreign", b"foreign"))
    connection.commit()
    connection.close()
    source = db.read_bytes()
    proof = RuntimeProof({}, "declaration", provenance=runtime)

    plan = adapter.plan(
        AdapterPlanContext("vscode", "core", {}, models=(model,)),
        proof,
        {
            _VSCODE_ARTIFACT: b'{"providers": []}\n',
            _VSCODE_STATE_ARTIFACTS[0]: source,
            _VSCODE_STATE_ARTIFACTS[1]: AbsentDestination(),
            _VSCODE_STATE_ARTIFACTS[2]: AbsentDestination(),
        },
        {},
    )

    planned_db = tmp_path / "planned.vscdb"
    planned_db.write_bytes(plan.artifacts[1].planned)
    connection = sqlite3.connect(planned_db)
    rows = dict(connection.execute("SELECT key, value FROM ItemTable"))
    connection.close()
    assert rows["foreign"] == b"foreign"
    assert rows["modfig:ModFig/router:primary"].startswith(b"v11")


def test_vscode_adapter_deletes_stale_owned_secret_rows(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    object.__setattr__(runtime, "secret_backend", lambda: b"key")
    object.__setattr__(runtime, "secret_values", {})
    connection = sqlite3.connect(tmp_path / "state.vscdb")
    connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
    connection.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("modfig:ModFig/old:removed", b"old-ciphertext"),
    )
    connection.commit()
    connection.close()
    proof = RuntimeProof({}, "declaration", provenance=runtime)
    plan = adapter.plan(
        AdapterPlanContext("vscode", "core", {}, models=()),
        proof,
        {
            _VSCODE_ARTIFACT: b'{"providers": []}\n',
            _VSCODE_STATE_ARTIFACTS[0]: (tmp_path / "state.vscdb").read_bytes(),
            _VSCODE_STATE_ARTIFACTS[1]: AbsentDestination(),
            _VSCODE_STATE_ARTIFACTS[2]: AbsentDestination(),
        },
        {"secretRowIds": ("modfig:ModFig/old:removed",)},
    )
    assert isinstance(plan.artifacts[1].planned, bytes)
    planned_db = tmp_path / "planned-stale.vscdb"
    planned_db.write_bytes(plan.artifacts[1].planned)
    connection = sqlite3.connect(planned_db)
    assert (
        connection.execute(
            "SELECT 1 FROM ItemTable WHERE key = ?", ("modfig:ModFig/old:removed",)
        ).fetchone()
        is None
    )
    connection.close()


def test_vscode_adapter_resolves_owned_secret_from_env_when_runtime_values_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    object.__setattr__(runtime, "secret_backend", lambda: b"key")
    monkeypatch.setenv("ROUTER_KEY", "secret-value")
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
    )
    connection = sqlite3.connect(tmp_path / "state.vscdb")
    connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
    connection.commit()
    connection.close()
    proof = RuntimeProof({}, "declaration", provenance=runtime)
    plan = adapter.plan(
        AdapterPlanContext("vscode", "core", {}, models=(model,)),
        proof,
        {
            _VSCODE_ARTIFACT: b'{"providers": []}\n',
            _VSCODE_STATE_ARTIFACTS[0]: (tmp_path / "state.vscdb").read_bytes(),
            _VSCODE_STATE_ARTIFACTS[1]: AbsentDestination(),
            _VSCODE_STATE_ARTIFACTS[2]: AbsentDestination(),
        },
        {},
    )
    assert plan.ownership["secretRowIds"] == ("modfig:ModFig/router:primary",)


def test_vscode_adapter_verifies_owned_secret_row(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    object.__setattr__(runtime, "secret_backend", lambda: b"key")
    object.__setattr__(runtime, "secret_values", {"ROUTER_KEY": b"secret-value"})
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
    )
    connection = sqlite3.connect(tmp_path / "state.vscdb")
    connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
    connection.execute("INSERT INTO ItemTable VALUES (?, ?)", ("foreign", b"foreign"))
    connection.commit()
    connection.close()
    proof = RuntimeProof({}, "declaration", provenance=runtime)
    context = AdapterPlanContext("vscode", "core", {}, models=(model,))
    plan = adapter.plan(
        context,
        proof,
        {
            _VSCODE_ARTIFACT: b'{"providers": []}\n',
            _VSCODE_STATE_ARTIFACTS[0]: (tmp_path / "state.vscdb").read_bytes(),
            _VSCODE_STATE_ARTIFACTS[1]: AbsentDestination(),
            _VSCODE_STATE_ARTIFACTS[2]: AbsentDestination(),
        },
        {},
    )

    adapter.verify(
        AdapterContext("vscode", "core"),
        proof,
        tuple(item.planned for item in plan.artifacts),
    )


def test_vscode_adapter_rejects_corrupt_owned_secret_ciphertext(tmp_path: Path) -> None:
    from modfig.clients.vscode.secrets import SecretContract, encode_secret

    runtime = _runtime(tmp_path)
    object.__setattr__(runtime, "secret_backend", lambda: b"key")
    object.__setattr__(runtime, "secret_values", {"ROUTER_KEY": b"secret-value"})
    connection = sqlite3.connect(tmp_path / "state.vscdb")
    connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
    connection.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        (
            "modfig:ModFig/router:primary",
            encode_secret(
                b"wrong",
                SecretContract("linux", "stable", "oscrypt-v11"),
                runtime.secret_backend,
            ),
        ),
    )
    connection.commit()
    connection.close()
    proof = RuntimeProof({}, "declaration", provenance=runtime)
    with pytest.raises(AppError, match="secret|decrypt|verification"):
        adapter.verify(
            AdapterContext("vscode", "core"),
            proof,
            (
                b'{"providers": []}\n',
                (tmp_path / "state.vscdb").read_bytes(),
                AbsentDestination(),
                AbsentDestination(),
            ),
        )


def test_vscode_adapter_rejects_absent_primary_database(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    proof = RuntimeProof({}, "declaration", provenance=runtime)
    with pytest.raises(Exception, match="state database"):
        adapter.plan(
            AdapterPlanContext("vscode", "core", {}, models=()),
            proof,
            {
                _VSCODE_ARTIFACT: b'{"providers": []}\n',
                ArtifactIdentity("vscode-state", PurePosixPath("state.vscdb")): AbsentDestination(),
                ArtifactIdentity(
                    "vscode-state", PurePosixPath("state.vscdb-wal")
                ): AbsentDestination(),
                ArtifactIdentity(
                    "vscode-state", PurePosixPath("state.vscdb-shm")
                ): AbsentDestination(),
            },
            {},
        )

    calls = 0

    def recheck() -> bool:
        nonlocal calls
        calls += 1
        return False

    runtime = _runtime(tmp_path)
    object.__setattr__(runtime, "runtime_recheck", recheck)
    proof = RuntimeProof({}, "declaration", provenance=runtime)

    with pytest.raises(AppError, match="quiescent|running"):
        adapter.recheck(proof)
    assert calls == 1


def test_vscode_preflight_rejects_insiders_runtime(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, channel="insiders")
    with pytest.raises(AppError, match="stable|channel"):
        preflight(runtime)
