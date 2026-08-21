from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from pathlib import Path

from .backup import BackupArtifact, BackupRequest, BackupSet, remove_backup_set, validate_backup_set
from .errors import AppError
from .journal import InvocationJournal, TransactionArtifact, validate_journal_bytes
from .locking import operation_lock, operation_locks
from .manifest import parse_ownership_manifest_bytes
from .storage import (
    FileVersion,
    conditional_delete,
    conditional_write_bytes,
    inspect_private_file,
    read_private_bytes,
)


class RecoveryResult(Enum):
    DISCARDED = "discarded"
    FINALIZED = "finalized"
    RESTORED = "restored"


def _state(current: FileVersion, before: FileVersion, after_sha256: str | None) -> str:
    if current == before:
        return "before"
    if current.sha256 == after_sha256 and current.exists == (after_sha256 is not None):
        return "after"
    return "unknown"


def _backup_request(journal: InvocationJournal) -> BackupRequest:
    return BackupRequest(
        journal.invocation_id,
        journal.backup_set,
        tuple(BackupArtifact(item.destination, item.before_version) for item in journal.artifacts),
    )


def _recover_transaction(
    journal_path: Path,
    backup_root: Path,
    *,
    trusted_manifest_path: Path | None = None,
    trusted_destinations: tuple[Path, ...] = (),
    trusted_destination_resolver: Callable[[TransactionArtifact], Path] | None = None,
) -> RecoveryResult:
    journal_path = journal_path.absolute()
    backup_root = backup_root.absolute()
    with operation_lock(journal_path, "recovery-preview"):
        preview_bytes = read_private_bytes(journal_path, "pending journal")
        preview = validate_journal_bytes(preview_bytes)
    if not trusted_destinations and trusted_destination_resolver is None:
        raise AppError("trusted destinations are required for pending journal recovery")
    if trusted_manifest_path is not None and preview.manifest_path.resolve(
        strict=False
    ) != trusted_manifest_path.resolve(strict=False):
        raise AppError("pending journal manifest path is not trusted")
    allowed = {path.resolve(strict=False) for path in trusted_destinations}
    for item in preview.artifacts:
        if trusted_destination_resolver is not None:
            try:
                trusted = trusted_destination_resolver(item).resolve(strict=False)
            except (AppError, KeyError, ValueError) as exc:
                raise AppError("pending journal destination is not trusted") from exc
            if item.destination.resolve(strict=False) != trusted:
                raise AppError("pending journal destination is not trusted")
        elif item.destination.resolve(strict=False) not in allowed:
            raise AppError("pending journal destination is not trusted")
    lock_paths = (preview.manifest_path, *(item.destination for item in preview.artifacts))
    with operation_locks(journal_path, iter(lock_paths), preview.invocation_id) as locks:
        locked_bytes = read_private_bytes(journal_path, "pending journal")
        journal = validate_journal_bytes(locked_bytes)
        if locked_bytes != preview_bytes or journal != preview:
            raise AppError("pending journal changed after preview; manual repair required")
        parse_ownership_manifest_bytes(
            journal.manifest_after_bytes, source="pending journal recovery"
        )
        journal_version = inspect_private_file(journal_path, "pending journal")
        manifest_version = inspect_private_file(journal.manifest_path, "manifest")
        manifest_state = _state(
            manifest_version, journal.manifest_before_version, journal.manifest_after_sha256
        )
        versions = {
            item.destination: inspect_private_file(item.destination, "recovery destination")
            for item in journal.artifacts
        }
        states = {
            item.destination: _state(
                versions[item.destination], item.before_version, item.after_sha256
            )
            for item in journal.artifacts
        }
        if manifest_state == "unknown" or "unknown" in states.values():
            raise AppError("pending journal contains unknown state; manual repair required")
        state_set = set(states.values())
        if not journal.artifacts:
            result, terminal = (
                (
                    RecoveryResult.DISCARDED
                    if manifest_state == "before"
                    else RecoveryResult.FINALIZED
                ),
                versions,
            )
        elif state_set == {"before"} and manifest_state == "before":
            result, terminal = RecoveryResult.DISCARDED, versions
        elif state_set == {"after"} and manifest_state in {"before", "after"}:
            if manifest_state == "before":
                manifest_version = conditional_write_bytes(
                    journal.manifest_path,
                    journal.manifest_after_bytes,
                    manifest_version,
                    "manifest",
                    writer_exclusion=locks[journal.manifest_path],
                )
            result, terminal = RecoveryResult.FINALIZED, versions
        elif state_set == {"before", "after"} and manifest_state == "before":
            prestates = validate_backup_set(
                BackupSet(backup_root / journal.backup_set, journal.backup_integrity),
                _backup_request(journal),
            )
            for item in journal.artifacts:
                content = prestates[item.destination]
                if content is None:
                    conditional_delete(
                        item.destination,
                        versions[item.destination],
                        "recovery destination",
                        writer_exclusion=locks[item.destination],
                    )
                else:
                    conditional_write_bytes(
                        item.destination,
                        content,
                        versions[item.destination],
                        "recovery destination",
                        writer_exclusion=locks[item.destination],
                    )
            terminal = {
                item.destination: inspect_private_file(item.destination, "recovery destination")
                for item in journal.artifacts
            }
            if any(
                terminal[item.destination].parent_identity != item.before_version.parent_identity
                or terminal[item.destination].exists != item.before_version.exists
                or terminal[item.destination].size != item.before_version.size
                or terminal[item.destination].sha256 != item.before_version.sha256
                for item in journal.artifacts
            ):
                raise AppError("recovery verification failed; manual repair required")
            result = RecoveryResult.RESTORED
        else:
            raise AppError("pending journal state is incomplete; manual repair required")
        if inspect_private_file(journal.manifest_path, "manifest") != manifest_version or any(
            inspect_private_file(path, "recovery destination") != version
            for path, version in terminal.items()
        ):
            raise AppError("destination changed before cleanup; manual repair required")
        conditional_delete(
            journal_path, journal_version, "pending journal", writer_exclusion=locks[journal_path]
        )
    if journal.artifacts:
        remove_backup_set(
            backup_root, BackupSet(backup_root / journal.backup_set, journal.backup_integrity)
        )
    return result


def recover_transaction(
    journal_path: Path,
    backup_root: Path,
    *,
    trusted_manifest_path: Path,
    trusted_destinations: tuple[Path, ...],
    trusted_destination_resolver: Callable[[TransactionArtifact], Path] | None = None,
) -> RecoveryResult:
    return _recover_transaction(
        journal_path,
        backup_root,
        trusted_manifest_path=trusted_manifest_path,
        trusted_destinations=trusted_destinations,
        trusted_destination_resolver=trusted_destination_resolver,
    )
