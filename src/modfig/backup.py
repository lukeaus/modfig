from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AppError
from .platform import open_private_parent, require_secure_io
from .storage import FileVersion, inspect_private_file, read_private_bytes

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORMAT_VERSION = 4


def _canonical(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()


@dataclass(frozen=True)
class BackupArtifact:
    destination: Path
    before_version: FileVersion

    def __post_init__(self) -> None:
        if not self.destination.is_absolute():
            raise AppError("backup artifact destination must be absolute")
        version = self.before_version
        if len(version.parent_identity) != 2 or any(v < 0 for v in version.parent_identity):
            raise AppError("backup artifact prestate parent identity is invalid")
        if version.exists:
            if (
                version.size is None
                or version.size < 0
                or version.sha256 is None
                or not _HEX_SHA256.fullmatch(version.sha256)
            ):
                raise AppError("backup artifact prestate is invalid")
        elif version.size is not None or version.sha256 is not None:
            raise AppError("backup absent artifact prestate is invalid")


@dataclass(frozen=True)
class BackupRequest:
    invocation_id: str
    set_id: str
    artifacts: tuple[BackupArtifact, ...]
    owner_readable_artifacts: frozenset[Path] = frozenset()

    def __post_init__(self) -> None:
        for value, label in ((self.invocation_id, "invocation id"), (self.set_id, "set id")):
            if not value or value in {".", ".."} or Path(value).name != value:
                raise AppError(f"backup {label} must be a safe path component")
        if not self.artifacts:
            raise AppError("backup request requires artifacts")
        if len({str(a.destination) for a in self.artifacts}) != len(self.artifacts):
            raise AppError("backup artifact destinations must be unique")
        destinations = {artifact.destination for artifact in self.artifacts}
        if not self.owner_readable_artifacts <= destinations:
            raise AppError("owner-readable backup paths must be requested artifacts")


@dataclass(frozen=True)
class BackupSet:
    path: Path
    integrity: str


def _validate_directory(fd: int, label: str) -> None:
    status = os.fstat(fd)
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.getuid()
        or status.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise AppError(f"{label} must be owner-only and owned by the current user")


def _open_directory(parent: int, name: str, label: str) -> int:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        _validate_directory(fd, label)
        return fd
    except OSError as exc:
        raise AppError(f"{label} is unsafe: {name}: {exc}") from exc


def _inspect(parent: int, name: str, label: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AppError(f"cannot inspect {label} {name}: {exc}") from exc


def _write_file(parent: int, name: str, content: bytes, label: str) -> None:
    fd: int | None = None
    try:
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(parent)
    except OSError as exc:
        raise AppError(f"cannot write {label}: {name}") from exc
    finally:
        if fd is not None:
            os.close(fd)


def _read_file(parent: int, name: str, label: str) -> bytes:
    status = _inspect(parent, name, label)
    if (
        status is None
        or stat.S_ISLNK(status.st_mode)
        or not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.getuid()
        or status.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise AppError(f"{label} is unsafe: {name}")
    fd: int | None = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (status.st_dev, status.st_ino):
            raise AppError(f"{label} changed while opening: {name}")
        with os.fdopen(fd, "rb") as handle:
            fd = None
            content = handle.read()
        if len(content) != opened.st_size:
            raise AppError(f"{label} changed while reading: {name}")
        return content
    except OSError as exc:
        raise AppError(f"cannot read {label}: {name}") from exc
    finally:
        if fd is not None:
            os.close(fd)


def _owner_readable_version(path: Path, label: str) -> FileVersion:
    parent = open_private_parent(path, label)
    descriptor = -1
    try:
        parent_status = os.fstat(parent)
        try:
            status = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return FileVersion((parent_status.st_dev, parent_status.st_ino), None, None, None)
        if (
            stat.S_ISLNK(status.st_mode)
            or not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.getuid()
            or status.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise AppError(f"{label} is unsafe")
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        opened = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            content = handle.read()
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            status.st_dev,
            status.st_ino,
            status.st_size,
        ) or len(content) != status.st_size:
            raise AppError(f"{label} changed while reading")
        return FileVersion(
            (parent_status.st_dev, parent_status.st_ino),
            (status.st_dev, status.st_ino),
            status.st_size,
            hashlib.sha256(content).hexdigest(),
        )
    except OSError as exc:
        raise AppError(f"{label} could not be read") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _read_owner_readable(path: Path, label: str) -> bytes:
    parent = open_private_parent(path, label)
    descriptor = -1
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            content = handle.read()
        return content
    except OSError as exc:
        raise AppError(f"{label} could not be read") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _root(path: Path) -> int:
    require_secure_io()
    fd = open_private_parent(path / ".root", "backup root", create=True)
    _validate_directory(fd, "backup root")
    return fd


def _remove_tree(parent: int, name: str) -> None:
    status = _inspect(parent, name, "backup cleanup entry")
    if status is None:
        return
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise AppError("backup cleanup entry is unsafe")
    child = _open_directory(parent, name, "backup cleanup directory")
    try:
        for item in os.listdir(child):
            item_status = _inspect(child, item, "backup cleanup child")
            if item_status is None:
                continue
            if stat.S_ISDIR(item_status.st_mode):
                _remove_tree(child, item)
            elif stat.S_ISREG(item_status.st_mode) and item_status.st_uid == os.getuid():
                os.unlink(item, dir_fd=child)
                os.fsync(child)
            else:
                raise AppError("backup cleanup child is unsafe")
    finally:
        os.close(child)
    os.rmdir(name, dir_fd=parent)
    os.fsync(parent)


def _prune(root: int, protected: frozenset[str]) -> None:
    sets: list[str] = []
    protected_count = 0
    for name in os.listdir(root):
        if name.startswith("."):
            continue
        status = _inspect(root, name, "retention entry")
        if status is None or not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
            continue
        if name in protected:
            protected_count += 1
        else:
            _validate_directory(_open_directory(root, name, "retention set"), "retention set")
            sets.append(name)
    for name in sorted(sets)[: max(0, len(sets) + protected_count - 10)]:
        _remove_tree(root, name)


def create_backup_set(
    backup_root: Path, request: BackupRequest, *, protected_set_ids: tuple[str, ...] = ()
) -> BackupSet:
    protected = frozenset(protected_set_ids) | {request.set_id}
    if len(protected) > 10:
        raise AppError("backup retention cannot protect more than 10 sets")
    prestates: list[bytes | None] = []
    for artifact in request.artifacts:
        owner_readable = artifact.destination in request.owner_readable_artifacts
        inspect = (
            _owner_readable_version
            if owner_readable
            else lambda path, label: inspect_private_file(path, label)
        )
        if inspect(artifact.destination, "backup source") != artifact.before_version:
            raise AppError("backup source prestate changed")
        if artifact.before_version.exists:
            source_content = (
                _read_owner_readable(artifact.destination, "backup source")
                if owner_readable
                else read_private_bytes(artifact.destination, "backup source")
            )
            if (
                len(source_content) != artifact.before_version.size
                or hashlib.sha256(source_content).hexdigest() != artifact.before_version.sha256
                or inspect(artifact.destination, "backup source") != artifact.before_version
            ):
                raise AppError("backup source prestate changed")
            prestates.append(source_content)
        else:
            prestates.append(None)
    root = _root(backup_root)
    temp = f".{request.set_id}.{secrets.token_hex(8)}"
    temp_fd: int | None = None
    published = False
    try:
        if _inspect(root, request.set_id, "backup set") is not None:
            raise AppError("backup set already exists")
        os.mkdir(temp, 0o700, dir_fd=root)
        os.fsync(root)
        temp_fd = _open_directory(root, temp, "temporary backup set")
        entries: list[dict[str, Any]] = []
        for index, (artifact, content) in enumerate(zip(request.artifacts, prestates, strict=True)):
            exists = content is not None
            blob = None if content is None else f"{index:04d}.bin"
            if content is not None:
                assert blob is not None
                _write_file(temp_fd, blob, content, "plaintext backup artifact")
            entries.append(
                {
                    "destination": str(artifact.destination),
                    "absent": not exists,
                    "sha256": None if content is None else hashlib.sha256(content).hexdigest(),
                    "length": None if content is None else len(content),
                    "blob": blob,
                }
            )
        payload = {
            "backupVersion": _FORMAT_VERSION,
            "invocationId": request.invocation_id,
            "setId": request.set_id,
            "artifacts": entries,
        }
        integrity = hashlib.sha256(_canonical(payload)).hexdigest()
        _write_file(
            temp_fd,
            "manifest.json",
            _canonical({"payload": payload, "integrity": integrity}) + b"\n",
            "plaintext backup manifest",
        )
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None
        os.replace(temp, request.set_id, src_dir_fd=root, dst_dir_fd=root)
        published = True
        os.fsync(root)
        _prune(root, protected)
        return BackupSet(backup_root / request.set_id, integrity)
    except Exception as exc:
        if temp_fd is not None:
            os.close(temp_fd)
        try:
            _remove_tree(root, request.set_id if published else temp)
        except Exception as cleanup_exc:
            raise AppError("backup creation failed and cleanup was unsafe") from cleanup_exc
        if isinstance(exc, AppError):
            raise
        raise AppError("cannot publish plaintext backup set") from exc
    finally:
        os.close(root)


def validate_backup_set(backup: BackupSet, request: BackupRequest) -> dict[Path, bytes | None]:
    if backup.path.absolute() != (backup.path.parent / request.set_id).absolute():
        raise AppError("backup set path does not match confined backup root")
    root = _root(backup.path.parent)
    try:
        set_fd = _open_directory(root, request.set_id, "backup set")
    finally:
        os.close(root)
    try:
        try:
            raw = json.loads(_read_file(set_fd, "manifest.json", "backup manifest"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AppError("backup manifest is corrupt") from exc
        if (
            not isinstance(raw, dict)
            or set(raw) != {"payload", "integrity"}
            or not isinstance(raw["payload"], dict)
        ):
            raise AppError("backup manifest is corrupt")
        payload, integrity = raw["payload"], raw["integrity"]
        if (
            payload.get("backupVersion") != _FORMAT_VERSION
            or payload.get("invocationId") != request.invocation_id
            or payload.get("setId") != request.set_id
            or not isinstance(payload.get("artifacts"), list)
            or not isinstance(integrity, str)
            or not _HEX_SHA256.fullmatch(integrity)
            or hashlib.sha256(_canonical(payload)).hexdigest() != integrity
            or integrity != backup.integrity
        ):
            raise AppError("backup manifest structural identity is corrupt")
        entries = payload["artifacts"]
        if len(entries) != len(request.artifacts):
            raise AppError("backup manifest artifact count is corrupt")
        result: dict[Path, bytes | None] = {}
        expected_names = {"manifest.json"}
        for index, (artifact, entry) in enumerate(zip(request.artifacts, entries, strict=True)):
            before = artifact.before_version
            expected = {
                "destination": str(artifact.destination),
                "absent": not before.exists,
                "sha256": before.sha256,
                "length": before.size,
                "blob": None if not before.exists else f"{index:04d}.bin",
            }
            if not isinstance(entry, dict) or set(entry) != set(expected) or entry != expected:
                raise AppError("backup artifact identity or version is corrupt")
            if before.exists:
                blob = expected["blob"]
                assert isinstance(blob, str)
                expected_names.add(blob)
                content = _read_file(set_fd, blob, "plaintext backup artifact")
                if (
                    len(content) != before.size
                    or hashlib.sha256(content).hexdigest() != before.sha256
                ):
                    raise AppError("plaintext backup artifact digest or length is corrupt")
                result[artifact.destination] = content
            else:
                result[artifact.destination] = None
        if set(os.listdir(set_fd)) != expected_names:
            raise AppError("backup set contains unknown or missing paths")
        return result
    finally:
        os.close(set_fd)


def remove_backup_set(backup_root: Path, backup: BackupSet) -> None:
    if backup.path.absolute() != (
        backup_root / backup.path.name
    ).absolute() or backup.path.name in {".", ".."}:
        raise AppError("backup set path does not match confined backup root")
    root = _root(backup_root)
    try:
        _remove_tree(root, backup.path.name)
    finally:
        os.close(root)
