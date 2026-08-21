from __future__ import annotations

import hashlib
import json
import plistlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modfig.clients.vscode import (
    VSCodeRuntimeFacts,
    capture_vscode_proof_record,
    contract_identity,
    load_vscode_runtime_proof,
    write_vscode_proof_record,
)
from modfig.errors import AppError


def record(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "proofVersion": 1,
        "binding": {
            "platform": "linux",
            "channel": "stable",
            "profileMode": "default",
            "version": "1.99.0",
            "build": "build-123",
            "bundleIdentity": "com.microsoft.VSCode",
            "executableSha256": "sha256:" + "a" * 64,
        },
        "contract": {
            "identity": contract_identity("linux", "oscrypt-v11"),
            "safeStorage": "proven",
            "keyContext": "proven",
            "secretFormat": "oscrypt-v11",
        },
        "capture": {
            "provenance": "read-only-installed-stable-code",
            "capturedAt": "2026-08-04T00:00:00Z",
            "freshUntil": "2026-08-05T00:00:00Z",
        },
    }
    value.update(changes)
    return value


def write_record(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def facts(**changes: str) -> VSCodeRuntimeFacts:
    value = {
        "platform": "linux",
        "channel": "stable",
        "version": "1.99.0",
        "build": "build-123",
        "bundle_identity": "com.microsoft.VSCode",
        "executable_sha256": "sha256:" + "a" * 64,
        "contract_identity": contract_identity("linux", "oscrypt-v11"),
        "secret_format": "oscrypt-v11",
    }
    value.update(changes)
    return VSCodeRuntimeFacts(
        value["platform"],
        value["channel"],
        value["version"],
        value["build"],
        value["bundle_identity"],
        value["executable_sha256"],
        value["contract_identity"],
        value["secret_format"],
    )


def test_loads_strict_record_and_matches_fresh_runtime(tmp_path: Path) -> None:
    path = tmp_path / "proof.json"
    write_record(path, record())
    proof = load_vscode_runtime_proof(
        path,
        home=tmp_path,
        os_name="linux",
        process_probe=lambda: True,
        now=datetime(2026, 8, 4, tzinfo=UTC),
        observe=facts,
    )
    assert proof.facts == {
        "channel": "stable",
        "platform": "linux",
        "version": "1.99.0",
        "build": "build-123",
        "bundleIdentity": "com.microsoft.VSCode",
        "executableSha256": "sha256:" + "a" * 64,
        "contractIdentity": contract_identity("linux", "oscrypt-v11"),
        "secretFormat": "oscrypt-v11",
    }
    assert str(tmp_path) not in json.dumps(dict(proof.facts))
    assert proof.provenance is not None and proof.provenance.secret_backend is not None


def test_loads_proof_with_explicit_user_data_root(tmp_path: Path) -> None:
    path = tmp_path / "proof.json"
    user_data_root = tmp_path / "custom" / "Code" / "User"
    write_record(path, record())

    proof = load_vscode_runtime_proof(
        path,
        home=tmp_path,
        user_data_root=user_data_root,
        os_name="linux",
        process_probe=lambda: True,
        now=datetime(2026, 8, 4, tzinfo=UTC),
        observe=facts,
    )

    runtime = proof.provenance
    assert runtime is not None
    assert runtime.settings_path == user_data_root / "chatLanguageModels.json"
    assert runtime.state_db_path == user_data_root / "globalStorage" / "state.vscdb"


@pytest.mark.parametrize(
    "value",
    [
        {"proofVersion": 1},
        {**record(), "unknown": True},
        {**record(), "binding": {**record()["binding"], "unknown": True}},
    ],
)
def test_rejects_missing_or_unknown_record_fields(tmp_path: Path, value: dict[str, object]) -> None:
    path = tmp_path / "proof.json"
    write_record(path, value)
    with pytest.raises(AppError, match="runtime proof is unavailable"):
        load_vscode_runtime_proof(path, home=tmp_path, os_name="linux")


def test_rejects_boolean_proof_version(tmp_path: Path) -> None:
    path = tmp_path / "proof.json"
    write_record(path, {**record(), "proofVersion": True})
    with pytest.raises(AppError, match="runtime proof is unavailable"):
        load_vscode_runtime_proof(path, home=tmp_path, os_name="linux")


def test_rejects_duplicate_record_fields(tmp_path: Path) -> None:
    path = tmp_path / "proof.json"
    path.write_text('{"proofVersion":1,"proofVersion":1}', encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(AppError, match="runtime proof is unavailable"):
        load_vscode_runtime_proof(path, home=tmp_path, os_name="linux")


@pytest.mark.parametrize(
    "change",
    [
        {"version": "1.99.1"},
        {"build": "other-build"},
        {"executableSha256": "not-a-sha256"},
    ],
)
def test_rejects_fresh_runtime_mismatch(tmp_path: Path, change: dict[str, str]) -> None:
    path = tmp_path / "proof.json"
    value = record()
    if "executableSha256" in change:
        value["binding"] = {**value["binding"], **change}
    write_record(path, value)
    with pytest.raises(AppError, match="runtime proof is unavailable"):
        load_vscode_runtime_proof(
            path,
            home=tmp_path,
            os_name="linux",
            process_probe=lambda: True,
            now=datetime(2026, 8, 4, tzinfo=UTC),
            observe=lambda: facts(**change) if "executableSha256" not in change else facts(),
        )


def test_rejects_non_rfc3339_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "proof.json"
    write_record(
        path,
        record(
            capture={
                "provenance": "read-only-installed-stable-code",
                "capturedAt": "2026-08-04 00:00:00+00:00",
                "freshUntil": "2026-08-05T00:00:00Z",
            }
        ),
    )
    with pytest.raises(AppError, match="runtime proof is unavailable"):
        load_vscode_runtime_proof(path, home=tmp_path, os_name="linux")


def test_rejects_stale_record(tmp_path: Path) -> None:
    path = tmp_path / "proof.json"
    write_record(
        path,
        record(
            capture={
                "provenance": "read-only-installed-stable-code",
                "capturedAt": "2026-08-01T00:00:00Z",
                "freshUntil": "2026-08-03T00:00:00Z",
            }
        ),
    )
    with pytest.raises(AppError, match="runtime proof is unavailable"):
        load_vscode_runtime_proof(
            path,
            home=tmp_path,
            os_name="linux",
            now=datetime(2026, 8, 4, tzinfo=UTC),
            observe=facts,
        )


def test_write_proof_record_is_private_and_loadable(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "proof.json"
    write_vscode_proof_record(record(), path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert (
        load_vscode_runtime_proof(
            path,
            home=tmp_path,
            os_name="linux",
            process_probe=lambda: True,
            now=datetime(2026, 8, 4, tzinfo=UTC),
            observe=facts,
        ).facts["platform"]
        == "linux"
    )


def test_capture_reads_metadata_without_running_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "modfig.clients.vscode._secret_backend",
        lambda _os_name: pytest.fail("injected backend should be used"),
    )
    install = (tmp_path / "code").resolve()
    executable = install / "code"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"stable-code")
    executable.chmod(0o700)
    product = install / "resources" / "app" / "product.json"
    product.parent.mkdir(parents=True)
    product.write_text(json.dumps({"version": "1.99.0", "commit": "build-123"}), encoding="utf-8")
    calls: list[str] = []
    result = capture_vscode_proof_record(
        home=tmp_path,
        os_name="linux",
        installation_root=install,
        process_probe=lambda: calls.append("process") or True,
        secret_backend=lambda: calls.append("key") or b"safe-storage-key",
        now=datetime(2026, 8, 4, tzinfo=UTC),
    )
    assert calls == ["process", "key"]
    assert result["binding"]["version"] == "1.99.0"
    assert result["binding"]["build"] == "build-123"
    assert (
        result["binding"]["executableSha256"]
        == "sha256:" + hashlib.sha256(b"stable-code").hexdigest()
    )
    assert result["contract"]["secretFormat"] == "oscrypt-v11"
    assert str(tmp_path) not in json.dumps(result)
    assert "safe-storage-key" not in json.dumps(result)


def test_capture_uses_oscrypt_v10_for_macos(tmp_path: Path) -> None:
    install = (tmp_path / "Visual Studio Code.app").resolve()
    executable = install / "Contents" / "MacOS" / "Code"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"stable-code")
    product = install / "Contents" / "Resources" / "app" / "product.json"
    product.parent.mkdir(parents=True)
    product.write_text(json.dumps({"version": "1.99.0", "commit": "build-123"}), encoding="utf-8")
    info = install / "Contents" / "Info.plist"
    info.write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.microsoft.VSCode"}))
    result = capture_vscode_proof_record(
        home=tmp_path,
        os_name="macos",
        installation_root=install,
        process_probe=lambda: True,
        secret_backend=lambda: b"safe-storage-key",
        now=datetime(2026, 8, 4, tzinfo=UTC),
    )
    assert result["contract"]["secretFormat"] == "oscrypt-v10"
    assert result["contract"]["identity"] == contract_identity("macos", "oscrypt-v10")


def test_capture_rejects_invalid_safe_storage_key(tmp_path: Path) -> None:
    install = (tmp_path / "code").resolve()
    executable = install / "code"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"stable-code")
    product = install / "resources" / "app" / "product.json"
    product.parent.mkdir(parents=True)
    product.write_text(json.dumps({"version": "1.99.0", "commit": "build-123"}), encoding="utf-8")
    with pytest.raises(AppError, match="runtime proof is unavailable"):
        capture_vscode_proof_record(
            os_name="linux",
            installation_root=install,
            process_probe=lambda: True,
            secret_backend=lambda: b"",
        )


def test_quiescence_recheck_reobserves_identity(tmp_path: Path) -> None:
    path = tmp_path / "proof.json"
    write_record(path, record())
    changed = {"value": False}

    def observe() -> VSCodeRuntimeFacts:
        return facts(build="other-build") if changed["value"] else facts()

    proof = load_vscode_runtime_proof(
        path,
        home=tmp_path,
        os_name="linux",
        process_probe=lambda: True,
        now=datetime(2026, 8, 4, tzinfo=UTC),
        observe=observe,
    )
    changed["value"] = True
    assert proof.provenance is not None and proof.provenance.runtime_recheck is not None
    assert proof.provenance.runtime_recheck() is False


def test_capture_rejects_symlinked_installation_ancestor(tmp_path: Path) -> None:
    real = tmp_path / "real"
    executable = real / "code"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"stable-code")
    product = real / "resources" / "app" / "product.json"
    product.parent.mkdir(parents=True)
    product.write_text(json.dumps({"version": "1.99.0", "commit": "build-123"}), encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(AppError, match="runtime proof is unavailable"):
        capture_vscode_proof_record(
            os_name="linux", installation_root=link, process_probe=lambda: True
        )


def test_capture_rejects_running_code_before_installation_read(tmp_path: Path) -> None:
    calls: list[str] = []
    with pytest.raises(AppError, match="runtime proof is unavailable"):
        capture_vscode_proof_record(
            home=tmp_path,
            os_name="linux",
            process_probe=lambda: calls.append("process") or False,
        )
    assert calls == ["process"]
