from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from modfig import app
from modfig.backup import BackupArtifact, BackupRequest, create_backup_set, validate_backup_set
from modfig.components import ExtensionComponent
from modfig.errors import AppError
from modfig.journal import (
    InvocationJournal,
    TransactionArtifact,
    journal_bytes,
    validate_journal_bytes,
)
from modfig.manifest import (
    AdapterProvenance,
    ClientOwnership,
    ComponentOwnership,
    OwnershipManifest,
    ownership_manifest_bytes,
)
from modfig.recovery import RecoveryResult, recover_transaction
from modfig.storage import FileVersion, inspect_private_file


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _journal_fixture(tmp_path: Path, *, manifest_path: Path, destination: Path) -> bytes:
    before_bytes = b"before"
    destination.write_bytes(before_bytes)
    destination.chmod(0o600)
    before = inspect_private_file(destination, "destination")
    after = b"after"
    artifact = TransactionArtifact(
        "factory",
        "core",
        "builtin.factory",
        "settings",
        PurePosixPath("settings.json"),
        destination,
        before,
        _sha(after),
    )
    manifest_before = ownership_manifest_bytes(OwnershipManifest())
    record = ComponentOwnership(
        "core",
        AdapterProvenance("builtin.factory", "modfig", "0.1"),
        "settings",
        PurePosixPath("settings.json"),
        _sha(before_bytes),
        _sha(after),
        {},
    )
    manifest_after = ownership_manifest_bytes(
        OwnershipManifest(clients={"factory": ClientOwnership((record,))})
    )
    return journal_bytes(
        InvocationJournal(
            "invoke-1",
            manifest_path,
            manifest_before,
            FileVersion((1, 2), (3, 4), len(manifest_before), _sha(manifest_before)),
            manifest_after,
            _sha(manifest_after),
            (artifact,),
            "set-1",
            "a" * 64,
        )
    )


def _pending_update(
    tmp_path: Path, *, destination_before: bytes | None = b"before"
) -> dict[str, Any]:
    destination = tmp_path / "settings.json"
    untouched = tmp_path / "untouched.json"
    after_bytes = b"after"
    if destination_before is not None:
        destination.write_bytes(destination_before)
        destination.chmod(0o600)
    untouched.write_bytes(b"untouched")
    untouched.chmod(0o600)
    before = inspect_private_file(destination, "destination")
    untouched_before = inspect_private_file(untouched, "destination")

    manifest_path = tmp_path / "manifest.json"
    manifest_before = ownership_manifest_bytes(OwnershipManifest())
    manifest_path.write_bytes(manifest_before)
    manifest_path.chmod(0o600)
    manifest_before_version = inspect_private_file(manifest_path, "manifest")
    records = (
        ComponentOwnership(
            "core",
            AdapterProvenance("builtin.factory", "modfig", "0.1"),
            "settings",
            PurePosixPath("settings.json"),
            None if destination_before is None else _sha(destination_before),
            _sha(after_bytes),
            {},
        ),
        ComponentOwnership(
            ExtensionComponent("oh-my-droid"),
            AdapterProvenance("builtin.factory", "modfig", "0.1"),
            "untouched",
            PurePosixPath("untouched.json"),
            _sha(b"untouched"),
            _sha(b"changed"),
            {},
        ),
    )
    manifest_after = ownership_manifest_bytes(
        OwnershipManifest(clients={"factory": ClientOwnership(records)})
    )
    artifacts = (
        TransactionArtifact(
            "factory",
            "core",
            "builtin.factory",
            "settings",
            PurePosixPath("settings.json"),
            destination,
            before,
            _sha(after_bytes),
        ),
        TransactionArtifact(
            "factory",
            ExtensionComponent("oh-my-droid"),
            "builtin.factory",
            "untouched",
            PurePosixPath("untouched.json"),
            untouched,
            untouched_before,
            _sha(b"changed"),
        ),
    )
    backup_root = tmp_path / "backups"
    backup_request = BackupRequest(
        "invoke-1",
        "set-1",
        tuple(BackupArtifact(item.destination, item.before_version) for item in artifacts),
    )
    backup = create_backup_set(backup_root, backup_request)
    journal = InvocationJournal(
        "invoke-1",
        manifest_path,
        manifest_before,
        manifest_before_version,
        manifest_after,
        _sha(manifest_after),
        artifacts,
        backup.path.name,
        backup.integrity,
    )
    journal_path = tmp_path / "pending.json"
    journal_path.write_bytes(journal_bytes(journal))
    journal_path.chmod(0o600)
    destination.write_bytes(after_bytes)
    destination.chmod(0o600)
    return {
        "destination": destination,
        "untouched": untouched,
        "before": destination_before,
        "after": after_bytes,
        "untouched_after": b"changed",
        "manifest": manifest_path,
        "manifest_before": manifest_before,
        "manifest_after": manifest_after,
        "journal": journal_path,
        "backup_root": backup_root,
        "backup": backup,
    }


def test_recovery_restored_existing_file_allows_immediate_retry_noop(tmp_path: Path) -> None:
    scenario = _pending_update(tmp_path)

    result = recover_transaction(
        scenario["journal"],
        scenario["backup_root"],
        trusted_manifest_path=scenario["manifest"],
        trusted_destinations=(scenario["destination"], scenario["untouched"]),
    )

    assert result is RecoveryResult.RESTORED
    assert scenario["destination"].read_bytes() == scenario["before"]
    assert scenario["manifest"].read_bytes() == scenario["manifest_before"]
    assert not scenario["journal"].exists()
    assert not scenario["backup"].path.exists()


def test_pending_recovery_retry_through_app_is_clean_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _pending_update(tmp_path)
    destinations = {
        "settings": scenario["destination"],
        "untouched": scenario["untouched"],
    }

    def trusted_destination(item: TransactionArtifact) -> Path:
        return destinations[item.grant_id]

    monkeypatch.setattr(app, "_trusted_recovery_destination", trusted_destination)
    monkeypatch.setattr(app, "resolve_manifest_path", lambda *_args: scenario["manifest"])
    monkeypatch.setattr(app, "load_valid_registry", lambda _config: object())
    monkeypatch.setattr(app, "selected_clients", lambda *_args: ())

    app._apply_transaction(
        None,
        "factory",
        True,
        journal_path=scenario["journal"],
        backup_root=scenario["backup_root"],
    )

    assert scenario["destination"].read_bytes() == scenario["before"]
    assert scenario["manifest"].read_bytes() == scenario["manifest_before"]
    assert not scenario["journal"].exists()
    assert not scenario["backup"].path.exists()


def test_recovery_restored_absent_file_removes_created_destination(tmp_path: Path) -> None:
    scenario = _pending_update(tmp_path, destination_before=None)

    result = recover_transaction(
        scenario["journal"],
        scenario["backup_root"],
        trusted_manifest_path=scenario["manifest"],
        trusted_destinations=(scenario["destination"], scenario["untouched"]),
    )

    assert result is RecoveryResult.RESTORED
    assert not scenario["destination"].exists()
    assert not scenario["journal"].exists()
    assert not scenario["backup"].path.exists()


def test_recovery_discards_all_before_state(tmp_path: Path) -> None:
    scenario = _pending_update(tmp_path)
    scenario["destination"].write_bytes(scenario["before"])

    result = recover_transaction(
        scenario["journal"],
        scenario["backup_root"],
        trusted_manifest_path=scenario["manifest"],
        trusted_destinations=(scenario["destination"], scenario["untouched"]),
    )

    assert result is RecoveryResult.DISCARDED
    assert not scenario["journal"].exists()
    assert not scenario["backup"].path.exists()


def test_recovery_finalized_cleans_pending_transaction(tmp_path: Path) -> None:
    scenario = _pending_update(tmp_path)
    scenario["destination"].write_bytes(scenario["after"])
    scenario["untouched"].write_bytes(scenario["untouched_after"])

    result = recover_transaction(
        scenario["journal"],
        scenario["backup_root"],
        trusted_manifest_path=scenario["manifest"],
        trusted_destinations=(scenario["destination"], scenario["untouched"]),
    )

    assert result is RecoveryResult.FINALIZED
    assert scenario["manifest"].read_bytes() == scenario["manifest_after"]
    assert not scenario["journal"].exists()
    assert not scenario["backup"].path.exists()


def test_recovery_unknown_state_retains_pending_transaction(tmp_path: Path) -> None:
    scenario = _pending_update(tmp_path)
    scenario["untouched"].write_bytes(b"concurrent-drift")

    with pytest.raises(AppError, match="unknown state"):
        recover_transaction(
            scenario["journal"],
            scenario["backup_root"],
            trusted_manifest_path=scenario["manifest"],
            trusted_destinations=(scenario["destination"], scenario["untouched"]),
        )

    assert scenario["untouched"].read_bytes() == b"concurrent-drift"
    assert scenario["journal"].exists()
    assert scenario["backup"].path.exists()


def test_recovery_rejects_valid_digest_journal_with_untrusted_manifest_path(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "settings.json"
    trusted_manifest = tmp_path / "manifest.json"
    journal_path = tmp_path / "pending.json"
    journal_path.write_bytes(
        _journal_fixture(
            tmp_path, manifest_path=tmp_path / "attacker.json", destination=destination
        )
    )
    journal_path.chmod(0o600)

    with pytest.raises(AppError, match="manifest path is not trusted"):
        recover_transaction(
            journal_path,
            tmp_path / "backups",
            trusted_manifest_path=trusted_manifest,
            trusted_destinations=(destination,),
        )

    assert journal_path.exists()
    assert not (tmp_path / "attacker.json").exists()


def test_recovery_requires_trusted_destination_bindings(
    tmp_path: Path,
) -> None:
    attacker_destination = tmp_path / "attacker.json"
    trusted_manifest = tmp_path / "manifest.json"
    journal_path = tmp_path / "pending.json"
    journal_path.write_bytes(
        _journal_fixture(
            tmp_path,
            manifest_path=trusted_manifest,
            destination=attacker_destination,
        )
    )
    journal_path.chmod(0o600)

    with pytest.raises(AppError, match="trusted destinations are required"):
        recover_transaction(
            journal_path,
            tmp_path / "backups",
            trusted_manifest_path=trusted_manifest,
            trusted_destinations=(),
        )

    assert journal_path.exists()


def test_recovery_rejects_valid_digest_journal_with_untrusted_destination(tmp_path: Path) -> None:
    trusted_destination = tmp_path / "settings.json"
    attacker_destination = tmp_path / "attacker.json"
    trusted_manifest = tmp_path / "manifest.json"
    journal_path = tmp_path / "pending.json"
    journal_path.write_bytes(
        _journal_fixture(
            tmp_path,
            manifest_path=trusted_manifest,
            destination=attacker_destination,
        )
    )
    journal_path.chmod(0o600)

    with pytest.raises(AppError, match="destination is not trusted"):
        recover_transaction(
            journal_path,
            tmp_path / "backups",
            trusted_manifest_path=trusted_manifest,
            trusted_destinations=(trusted_destination,),
        )

    assert journal_path.exists()
    assert attacker_destination.read_bytes() == b"before"


def test_recovery_rejects_valid_digest_journal_path_escape_from_trusted_root(
    tmp_path: Path,
) -> None:
    trusted_root = tmp_path / "trusted"
    trusted_root.mkdir()
    escaped_destination = trusted_root / ".." / "attacker.json"
    trusted_manifest = tmp_path / "manifest.json"
    journal_path = tmp_path / "pending.json"
    journal_path.write_bytes(
        _journal_fixture(
            tmp_path,
            manifest_path=trusted_manifest,
            destination=escaped_destination,
        )
    )
    journal_path.chmod(0o600)

    with pytest.raises(AppError, match="destination is not trusted"):
        recover_transaction(
            journal_path,
            tmp_path / "backups",
            trusted_manifest_path=trusted_manifest,
            trusted_destinations=(),
            trusted_destination_resolver=lambda _item: tmp_path / "trusted" / "settings.json",
        )

    assert journal_path.exists()
    assert (tmp_path / "attacker.json").read_bytes() == b"before"


def test_recovery_rejects_valid_digest_destination_without_exact_grant_binding(
    tmp_path: Path,
) -> None:
    trusted_destination = tmp_path / "settings.json"
    attacker_destination = tmp_path / "attacker.json"
    trusted_manifest = tmp_path / "manifest.json"
    journal_path = tmp_path / "pending.json"
    journal_path.write_bytes(
        _journal_fixture(
            tmp_path,
            manifest_path=trusted_manifest,
            destination=attacker_destination,
        )
    )
    journal_path.chmod(0o600)

    with pytest.raises(AppError, match="destination is not trusted"):
        recover_transaction(
            journal_path,
            tmp_path / "backups",
            trusted_manifest_path=trusted_manifest,
            trusted_destinations=(),
            trusted_destination_resolver=lambda _item: trusted_destination,
        )

    assert journal_path.exists()
    assert attacker_destination.read_bytes() == b"before"


def test_plaintext_backup_round_trip_owner_only(tmp_path: Path) -> None:
    destination = tmp_path / "settings.json"
    destination.write_bytes(b"secret-state")
    destination.chmod(0o600)
    before = inspect_private_file(destination, "destination")
    request = BackupRequest("invoke-1", "set-1", (BackupArtifact(destination, before),))
    backup = create_backup_set(tmp_path / "backups", request)
    assert (backup.path / "0000.bin").read_bytes() == b"secret-state"
    assert (backup.path / "0000.bin").stat().st_mode & 0o777 == 0o600
    assert backup.path.stat().st_mode & 0o777 == 0o700
    assert validate_backup_set(backup, request) == {destination: b"secret-state"}


def test_plaintext_backup_rejects_legacy_format(tmp_path: Path) -> None:
    destination = tmp_path / "settings.json"
    destination.write_bytes(b"before")
    destination.chmod(0o600)
    before = inspect_private_file(destination, "destination")
    request = BackupRequest("invoke-1", "set-1", (BackupArtifact(destination, before),))
    backup = create_backup_set(tmp_path / "backups", request)
    manifest = backup.path / "manifest.json"
    manifest.write_text('{"journalVersion":3,"payload":{},"authentication":"x"}')
    try:
        validate_backup_set(backup, request)
    except Exception as exc:
        assert "manifest" in str(exc) or "structural" in str(exc)
    else:
        raise AssertionError("legacy backup was accepted")


def test_plaintext_journal_round_trip_and_digest_binding(tmp_path: Path) -> None:
    destination = (tmp_path / "settings.json").absolute()
    before_bytes = b"before"
    destination.write_bytes(before_bytes)
    destination.chmod(0o600)
    before = inspect_private_file(destination, "destination")
    after = b"after"
    artifact = TransactionArtifact(
        "factory",
        "core",
        "builtin.factory",
        "settings",
        PurePosixPath("settings.json"),
        destination,
        before,
        _sha(after),
    )
    manifest_before = ownership_manifest_bytes(OwnershipManifest())
    record = ComponentOwnership(
        "core",
        AdapterProvenance("builtin.factory", "modfig", "0.1"),
        "settings",
        PurePosixPath("settings.json"),
        _sha(before_bytes),
        _sha(after),
        {},
    )
    manifest_after = ownership_manifest_bytes(
        OwnershipManifest(clients={"factory": ClientOwnership((record,))})
    )
    journal = InvocationJournal(
        "invoke-1",
        (tmp_path / "manifest.json").absolute(),
        manifest_before,
        FileVersion((1, 2), (3, 4), len(manifest_before), _sha(manifest_before)),
        manifest_after,
        _sha(manifest_after),
        (artifact,),
        "set-1",
        "a" * 64,
    )
    encoded = journal_bytes(journal)
    assert validate_journal_bytes(encoded) == journal
    assert b"before" in encoded
