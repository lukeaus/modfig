from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from modfig.backup import (
    BackupArtifact,
    BackupRequest,
    create_backup_set,
    remove_backup_set,
    validate_backup_set,
)
from modfig.clients.factory.extensions.oh_my_droid import snapshot_droid_file
from modfig.errors import AppError
from modfig.storage import inspect_private_file


def _backup(tmp_path: Path):
    destination = tmp_path / "settings.json"
    destination.write_bytes(b"before")
    destination.chmod(0o600)
    request = BackupRequest(
        "invoke-1",
        "set-1",
        (BackupArtifact(destination, inspect_private_file(destination, "destination")),),
    )
    return destination, request, create_backup_set(tmp_path / "backups", request)


def _rewrite_manifest(backup_path: Path, mutate) -> None:
    manifest = backup_path / "manifest.json"
    raw = json.loads(manifest.read_bytes())
    mutate(raw["payload"])
    raw["integrity"] = hashlib.sha256(
        json.dumps(raw["payload"], separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    manifest.write_bytes((json.dumps(raw, separators=(",", ":"), sort_keys=True) + "\n").encode())
    manifest.chmod(0o600)


def test_plaintext_backup_round_trip_and_permissions(tmp_path: Path) -> None:
    destination = tmp_path / "settings.json"
    destination.write_bytes(b"before")
    destination.chmod(0o600)
    request = BackupRequest(
        "invoke-1",
        "set-1",
        (BackupArtifact(destination, inspect_private_file(destination, "destination")),),
    )
    backup = create_backup_set(tmp_path / "backups", request)
    assert (backup.path / "0000.bin").read_bytes() == b"before"
    assert (backup.path / "0000.bin").stat().st_mode & 0o777 == 0o600
    assert backup.path.stat().st_mode & 0o777 == 0o700
    assert validate_backup_set(backup, request) == {destination: b"before"}
    remove_backup_set(tmp_path / "backups", backup)
    assert not backup.path.exists()


def test_owner_readable_backup_source_is_supported(tmp_path: Path) -> None:
    destination = tmp_path / "droid.md"
    destination.write_bytes(b"legacy")
    destination.chmod(0o644)
    _content, version = snapshot_droid_file(destination)
    request = BackupRequest(
        "invoke-1",
        "set-1",
        (BackupArtifact(destination, version),),
        frozenset({destination}),
    )
    backup = create_backup_set(tmp_path / "backups", request)
    assert validate_backup_set(backup, request) == {destination: b"legacy"}


def test_plaintext_backup_preserves_absent_prestates(tmp_path: Path) -> None:
    destination = tmp_path / "new.json"
    request = BackupRequest(
        "invoke-1",
        "set-1",
        (BackupArtifact(destination, inspect_private_file(destination, "destination")),),
    )
    backup = create_backup_set(tmp_path / "backups", request)
    assert validate_backup_set(backup, request) == {destination: None}


def test_retention_preserves_pending_referenced_set(tmp_path: Path) -> None:
    destination = tmp_path / "settings.json"
    destination.write_bytes(b"before")
    destination.chmod(0o600)
    backup_root = tmp_path / "backups"
    # A structurally valid pending journal references the oldest set.  The
    # retention pass must treat that reference as protected state.
    for index in range(10):
        request = BackupRequest(
            f"invoke-{index}",
            f"set-{index:02d}",
            (BackupArtifact(destination, inspect_private_file(destination, "destination")),),
        )
        create_backup_set(backup_root, request)
    (tmp_path / "pending.json").write_text('{"payload":{"backupSet":"set-00"}}', encoding="utf-8")
    request = BackupRequest(
        "invoke-new",
        "set-new",
        (BackupArtifact(destination, inspect_private_file(destination, "destination")),),
    )
    create_backup_set(backup_root, request, protected_set_ids=("set-00",))
    assert (backup_root / "set-00").exists()
    assert len([item for item in backup_root.iterdir() if item.is_dir()]) <= 10
    assert (backup_root / "set-new").exists()


def test_retention_rejects_more_than_ten_protected_pending_sets(tmp_path: Path) -> None:
    destination = tmp_path / "settings.json"
    destination.write_bytes(b"before")
    destination.chmod(0o600)
    backup_root = tmp_path / "backups"
    pending_set_ids = tuple(f"pending-{index:02d}" for index in range(10))
    for index, set_id in enumerate(pending_set_ids):
        create_backup_set(
            backup_root,
            BackupRequest(
                f"invoke-{index}",
                set_id,
                (BackupArtifact(destination, inspect_private_file(destination, "destination")),),
            ),
        )

    with pytest.raises(AppError, match="cannot protect more than 10"):
        create_backup_set(
            backup_root,
            BackupRequest(
                "invoke-new",
                "set-new",
                (BackupArtifact(destination, inspect_private_file(destination, "destination")),),
            ),
            protected_set_ids=pending_set_ids,
        )

    assert {item.name for item in backup_root.iterdir() if item.is_dir()} == set(pending_set_ids)


def test_retention_protects_explicit_pending_set_beyond_ten(tmp_path: Path) -> None:
    destination = tmp_path / "settings.json"
    destination.write_bytes(b"before")
    destination.chmod(0o600)
    backup_root = tmp_path / "backups"
    for index in range(11):
        request = BackupRequest(
            f"invoke-{index}",
            f"set-{index:02d}",
            (BackupArtifact(destination, inspect_private_file(destination, "destination")),),
        )
        create_backup_set(backup_root, request, protected_set_ids=("set-00",))
    assert (backup_root / "set-00").exists()
    assert (backup_root / "set-10").exists()
    assert len([item for item in backup_root.iterdir() if item.is_dir()]) <= 10


@pytest.mark.parametrize("content", [b"change", b"changed"])
def test_backup_blob_digest_or_length_mismatch_is_rejected(tmp_path: Path, content: bytes) -> None:
    _destination, request, backup = _backup(tmp_path)
    (backup.path / "0000.bin").write_bytes(content)

    with pytest.raises(AppError, match="digest or length is corrupt"):
        validate_backup_set(backup, request)


def test_backup_version_mismatch_is_rejected(tmp_path: Path) -> None:
    _destination, request, backup = _backup(tmp_path)
    _rewrite_manifest(backup.path, lambda payload: payload.__setitem__("backupVersion", 99))

    with pytest.raises(AppError, match="structural identity is corrupt"):
        validate_backup_set(backup, request)


def test_backup_unknown_path_is_rejected(tmp_path: Path) -> None:
    _destination, request, backup = _backup(tmp_path)
    extra = backup.path / "unexpected.bin"
    extra.write_bytes(b"x")
    extra.chmod(0o600)

    with pytest.raises(AppError, match="unknown or missing paths"):
        validate_backup_set(backup, request)
