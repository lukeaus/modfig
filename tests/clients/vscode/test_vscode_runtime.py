from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from modfig.clients.vscode import (
    VSCodeRuntime,
    acquire_vscode_runtime,
    bind_vscode_runtime_paths,
    discover_vscode_runtime,
    discover_vscode_user_data_root,
    preflight,
    resolve_vscode_runtime,
)
from modfig.errors import AppError


def runtime_record(tmp_path: Path, **overrides: object) -> dict[str, object]:
    user_root = tmp_path / "User"
    record: dict[str, object] = {
        "supportedOs": ["macos", "linux"],
        "supportedChannels": ["stable", "insiders"],
        "supportedProfileModes": ["default", "profile", "portable"],
        "userDataRoot": str(user_root),
        "settingsPath": str(user_root / "chatLanguageModels.json"),
        "stateDbPath": str(user_root / "globalStorage" / "state.vscdb"),
        "stateWalPath": str(user_root / "globalStorage" / "state.vscdb-wal"),
        "stateShmPath": str(user_root / "globalStorage" / "state.vscdb-shm"),
        "stateDatabaseMembers": ["state.vscdb", "state.vscdb-wal", "state.vscdb-shm"],
        "itemTable": {
            "access": "proof-recorded-rows-only",
            "unknownRows": "preserve-without-inspection",
        },
        "safeStorage": "proven",
        "keyContext": "proven",
        "processDetector": "proven",
        "processQuiescent": True,
        "vendorApiTypeMapping": False,
    }
    record.update(overrides)
    return record


@pytest.mark.parametrize(
    ("os_name", "expected"),
    [
        ("darwin", "Library/Application Support/Code/User"),
        ("macos", "Library/Application Support/Code/User"),
        ("linux", ".config/Code/User"),
    ],
)
def test_discover_vscode_user_data_root_uses_platform_path(
    tmp_path: Path, os_name: str, expected: str
) -> None:
    assert discover_vscode_user_data_root(home=tmp_path, os_name=os_name) == tmp_path / expected


def test_default_process_probe_checks_electron_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr("modfig.clients.vscode.subprocess.run", fake_run)
    from modfig.clients.vscode import _default_process_probe

    assert _default_process_probe("macos") is True
    assert calls == [["pgrep", "-x", "Electron"], ["pgrep", "-x", "Code"]]


def test_default_process_probe_checks_code_names_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0 if command[-1] == "code" else 1)

    monkeypatch.setattr("modfig.clients.vscode.subprocess.run", fake_run)
    from modfig.clients.vscode import _default_process_probe

    assert _default_process_probe("linux") is False
    assert calls == [["pgrep", "-x", "code"], ["pgrep", "-x", "Code"]]


def test_discover_vscode_user_data_root_rejects_unsupported_platform(tmp_path: Path) -> None:
    with pytest.raises(AppError, match="platform|unsupported"):
        discover_vscode_user_data_root(home=tmp_path, os_name="windows")


def test_discover_vscode_runtime_requires_injected_quiescence_probe(tmp_path: Path) -> None:
    with pytest.raises(AppError, match="quiescen|probe"):
        discover_vscode_runtime(home=tmp_path, process_probe=lambda: False)


def test_discover_vscode_runtime_checks_quiescence_before_safe_storage(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class Backend:
        def key_bytes(self) -> bytes:
            calls.append("secret")
            raise AssertionError("safe storage must not be read while Code is running")

    with pytest.raises(AppError, match="quiescen|running"):
        discover_vscode_runtime(
            home=tmp_path,
            os_name="linux",
            process_probe=lambda: False,
            secret_backend=Backend(),
        )
    assert calls == []

    backend = type("Backend", (), {"key_bytes": lambda self: b"key"})()
    runtime = discover_vscode_runtime(
        home=tmp_path,
        os_name="linux",
        process_probe=lambda: True,
        secret_backend=backend,
        secret_format="oscrypt-v11",
    )

    user_root = tmp_path / ".config" / "Code" / "User"
    assert runtime.user_data_root == user_root
    assert runtime.settings_path == user_root / "chatLanguageModels.json"
    assert runtime.state_db_path == user_root / "globalStorage" / "state.vscdb"
    assert runtime.state_wal_path == user_root / "globalStorage" / "state.vscdb-wal"
    assert runtime.state_shm_path == user_root / "globalStorage" / "state.vscdb-shm"
    assert runtime.supported_channels == ("stable",)
    assert runtime.supported_profile_modes == ("default",)
    assert runtime.secret_backend is backend


def test_bind_vscode_runtime_paths_rejects_route_mismatch(tmp_path: Path) -> None:
    runtime = resolve_vscode_runtime(
        runtime_record(tmp_path), os_name="macos", channel="stable", profile_mode="default"
    )
    with pytest.raises(AppError, match="path|destination"):
        bind_vscode_runtime_paths(
            runtime,
            settings_path=tmp_path / "other" / "chatLanguageModels.json",
            state_db_path=runtime.state_db_path,
            state_wal_path=runtime.state_wal_path,
            state_shm_path=runtime.state_shm_path,
        )


def test_bind_vscode_runtime_paths_returns_the_proof_bound_runtime(tmp_path: Path) -> None:
    runtime = resolve_vscode_runtime(
        runtime_record(tmp_path), os_name="macos", channel="stable", profile_mode="default"
    )

    assert (
        bind_vscode_runtime_paths(
            runtime,
            settings_path=runtime.settings_path,
            state_db_path=runtime.state_db_path,
            state_wal_path=runtime.state_wal_path,
            state_shm_path=runtime.state_shm_path,
        )
        is runtime
    )


def test_acquire_vscode_runtime_keeps_injected_runtime_seams(tmp_path: Path) -> None:
    backend = object()

    def process_probe() -> bool:
        return True

    runtime = acquire_vscode_runtime(
        runtime_record(tmp_path),
        os_name="macos",
        channel="stable",
        profile_mode="default",
        secret_backend=backend,
        process_probe=process_probe,
    )

    assert runtime.secret_backend is backend
    assert runtime.runtime_recheck is process_probe


def test_acquire_vscode_runtime_rejects_running_process_probe(tmp_path: Path) -> None:
    with pytest.raises(AppError, match="quiescen|running"):
        acquire_vscode_runtime(
            runtime_record(tmp_path),
            os_name="macos",
            channel="stable",
            profile_mode="default",
            process_probe=lambda: False,
        )


def test_acquire_vscode_runtime_fails_when_recorded_process_is_running(tmp_path: Path) -> None:
    with pytest.raises(AppError, match="quiescen|running"):
        acquire_vscode_runtime(
            runtime_record(tmp_path, processQuiescent=False),
            os_name="macos",
            channel="stable",
            profile_mode="default",
        )


def test_resolve_vscode_runtime_accepts_complete_proven_contract(tmp_path: Path) -> None:
    runtime = resolve_vscode_runtime(
        runtime_record(tmp_path),
        os_name="macos",
        channel="stable",
        profile_mode="default",
    )

    assert isinstance(runtime, VSCodeRuntime)
    assert runtime.supported_os == ("macos", "linux")
    assert runtime.supported_channels == ("stable", "insiders")
    assert runtime.supported_profile_modes == ("default", "profile", "portable")
    assert runtime.user_data_root == tmp_path / "User"
    assert runtime.settings_path == tmp_path / "User" / "chatLanguageModels.json"
    assert runtime.state_db_path.name == "state.vscdb"
    assert runtime.state_wal_path.name == "state.vscdb-wal"
    assert runtime.state_shm_path.name == "state.vscdb-shm"
    assert runtime.safe_storage_supported is True
    assert runtime.key_context == "proven"
    assert runtime.process_quiescent is True
    assert runtime.os_name == "macos"
    assert runtime.channel == "stable"
    assert runtime.profile_mode == "default"
    assert runtime.secret_format == "oscrypt-v10"
    assert runtime.vendor_api_type_mapping is False
    assert runtime.runtime_probe is not None
    assert runtime.runtime_recheck is not None
    assert preflight(runtime) == runtime


def test_resolve_vscode_runtime_rejects_windows_static_record(tmp_path: Path) -> None:
    with pytest.raises(AppError, match="operating system|proof-supported"):
        resolve_vscode_runtime(
            runtime_record(tmp_path, supportedOs=["windows"]),
            os_name="windows",
            channel="stable",
            profile_mode="default",
        )


@pytest.mark.parametrize(
    ("change", "os_name", "channel", "profile_mode", "message"),
    [
        ({"supportedOs": ["linux"]}, "macos", "stable", "default", "operating system"),
        ({"supportedChannels": ["insiders"]}, "macos", "stable", "default", "channel"),
        ({"supportedProfileModes": ["default"]}, "macos", "stable", "profile", "profile"),
        ({"userDataRoot": "relative/path"}, "macos", "stable", "default", "absolute"),
        ({"userDataRoot": ""}, "macos", "stable", "default", "user data root"),
        ({"settingsPath": "relative.json"}, "macos", "stable", "default", "absolute"),
        ({"settingsPath": ""}, "macos", "stable", "default", "settings path"),
        ({"stateDbPath": "relative.vscdb"}, "macos", "stable", "default", "absolute"),
        ({"stateWalPath": "relative.vscdb-wal"}, "macos", "stable", "default", "absolute"),
        ({"stateShmPath": "relative.vscdb-shm"}, "macos", "stable", "default", "absolute"),
        (
            {"stateDatabaseMembers": ["state.vscdb", "state.vscdb-wal"]},
            "macos",
            "stable",
            "default",
            "state database members",
        ),
        (
            {"stateDatabaseMembers": ["state.vscdb", "state.vscdb-wal", "extra.db"]},
            "macos",
            "stable",
            "default",
            "state database members",
        ),
        ({"processQuiescent": False}, "macos", "stable", "default", "quiescen"),
        ({"safeStorage": "unproven"}, "macos", "stable", "default", "safeStorage"),
        ({"keyContext": "unproven"}, "macos", "stable", "default", "keyContext"),
        ({"processDetector": "unproven"}, "macos", "stable", "default", "processDetector"),
    ],
)
def test_resolve_vscode_runtime_rejects_unproven_platform_contracts(
    tmp_path: Path,
    change: dict[str, object],
    os_name: str,
    channel: str,
    profile_mode: str,
    message: str,
) -> None:
    record = runtime_record(tmp_path, **change)

    with pytest.raises(AppError, match=message):
        resolve_vscode_runtime(record, os_name=os_name, channel=channel, profile_mode=profile_mode)


def test_resolve_vscode_runtime_rejects_itemtable_access_not_proof_recorded(
    tmp_path: Path,
) -> None:
    record = runtime_record(tmp_path, itemTable={"access": "enumerated", "unknownRows": "drop"})

    with pytest.raises(AppError, match="itemTable"):
        resolve_vscode_runtime(record, os_name="macos", channel="stable", profile_mode="default")


def test_resolve_vscode_runtime_rejects_unknown_rows_not_preserved(tmp_path: Path) -> None:
    record = runtime_record(
        tmp_path,
        itemTable={"access": "proof-recorded-rows-only", "unknownRows": "delete-unknown"},
    )

    with pytest.raises(AppError, match="itemTable"):
        resolve_vscode_runtime(record, os_name="macos", channel="stable", profile_mode="default")


def test_preflight_without_runtime_proof_remains_fail_closed() -> None:
    with pytest.raises(AppError) as exc_info:
        preflight()
    assert exc_info.value.exit_code == 1


def test_preflight_rejects_runtime_without_safe_storage_context() -> None:
    runtime = VSCodeRuntime(
        supported_os=("macos",),
        supported_channels=("stable",),
        supported_profile_modes=("default",),
        user_data_root=Path.cwd() / ".modfig-test" / "User",
        settings_path=Path.cwd() / ".modfig-test" / "User" / "chatLanguageModels.json",
        state_db_path=Path.cwd() / ".modfig-test" / "state.vscdb",
        state_wal_path=Path.cwd() / ".modfig-test" / "state.vscdb-wal",
        state_shm_path=Path.cwd() / ".modfig-test" / "state.vscdb-shm",
        safe_storage_supported=False,
        key_context="",
        process_quiescent=True,
        vendor_api_type_mapping=False,
    )

    with pytest.raises(AppError, match="safeStorage|key context"):
        preflight(runtime)


def test_preflight_rejects_runtime_without_quiescence() -> None:
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
        process_quiescent=False,
        vendor_api_type_mapping=False,
    )

    with pytest.raises(AppError, match="running|quiescen"):
        preflight(runtime)
