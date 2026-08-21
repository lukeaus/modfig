from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .errors import AppError
from .locking import OperationLock, operation_lock
from .platform import (
    CapabilityUnavailableError,
    PrivateParentMissingError,
    open_private_parent,
    platform_capabilities,
    require_secure_io,
)

DEFAULT_CONFIG_NAME = ".modfig.yaml"
XDG_CONFIG_RELATIVE = Path(".config") / "modfig" / "config.yaml"
ConfigPathSource = Literal["explicit", "environment", "xdg", "legacy"]


class ConcurrentModificationError(AppError):
    """The destination no longer matches the exact bytes inspected by the caller."""


@dataclass(frozen=True)
class FileVersion:
    parent_identity: tuple[int, int]
    leaf_identity: tuple[int, int] | None
    size: int | None
    sha256: str | None

    @property
    def exists(self) -> bool:
        return self.leaf_identity is not None


@dataclass(frozen=True)
class ConfigPathResolution:
    path: Path
    source: ConfigPathSource


def resolve_config_path(
    configured: str | None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> ConfigPathResolution:
    environment: Mapping[str, str] = os.environ if environ is None else environ
    home_path = Path.home() if home is None else home
    if configured:
        return ConfigPathResolution(Path(configured).expanduser(), "explicit")
    env_value = environment.get("MODFIG_CONFIG")
    if env_value:
        return ConfigPathResolution(Path(env_value).expanduser(), "environment")
    xdg_value = environment.get("XDG_CONFIG_HOME")
    xdg_path = Path(xdg_value).expanduser() if xdg_value else None
    if xdg_path is not None and xdg_path.is_absolute():
        current = xdg_path / "modfig" / "config.yaml"
    else:
        current = home_path / XDG_CONFIG_RELATIVE
    legacy = home_path / DEFAULT_CONFIG_NAME
    if not current.exists() and legacy.exists():
        return ConfigPathResolution(legacy, "legacy")
    return ConfigPathResolution(current, "xdg")


def _open_private_parent(path: Path, label: str) -> int:
    return open_private_parent(path, label)


def _read_private_at(
    parent_descriptor: int, path: Path, label: str, *, missing_ok: bool
) -> tuple[os.stat_result, bytes] | None:
    file_descriptor: int | None = None
    try:
        try:
            expected = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise AppError(f"cannot read {label} {path}: file does not exist") from None
        except OSError as exc:
            raise AppError(f"cannot read {label} {path}: {exc}") from exc
        if stat.S_ISLNK(expected.st_mode):
            raise AppError(f"{label} must not be a symlink: {path}")
        if not stat.S_ISREG(expected.st_mode):
            raise AppError(f"{label} is not a regular file: {path}")
        if expected.st_uid != os.getuid():
            raise AppError(f"{label} must be owned by the current user: {path}")
        if expected.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise AppError(
                f"{label} must be owner-only and not writable by group or others: {path}"
            )
        try:
            file_descriptor = os.open(
                path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise AppError(f"cannot read {label} {path}: {exc}") from exc
        opened = os.fstat(file_descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise AppError(f"{label} changed while opening: {path}")
        with os.fdopen(file_descriptor, "rb") as handle:
            file_descriptor = None
            content = handle.read()
        if len(content) != opened.st_size:
            raise AppError(f"{label} changed while reading: {path}")
        return opened, content
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)


def _version_at(parent_descriptor: int, path: Path, label: str) -> FileVersion:
    parent = os.fstat(parent_descriptor)
    result = _read_private_at(parent_descriptor, path, label, missing_ok=True)
    if result is None:
        return FileVersion((parent.st_dev, parent.st_ino), None, None, None)
    status, content = result
    return FileVersion(
        (parent.st_dev, parent.st_ino),
        (status.st_dev, status.st_ino),
        len(content),
        hashlib.sha256(content).hexdigest(),
    )


def inspect_private_file(path: Path, label: str) -> FileVersion:
    parent_descriptor = _open_private_parent(path, label)
    try:
        return _version_at(parent_descriptor, path, label)
    finally:
        os.close(parent_descriptor)


def read_private_bytes(path: Path, label: str) -> bytes:
    parent_descriptor = _open_private_parent(path, label)
    try:
        result = _read_private_at(parent_descriptor, path, label, missing_ok=False)
        assert result is not None
        return result[1]
    finally:
        os.close(parent_descriptor)


def read_private_text(path: Path, label: str) -> str:
    try:
        return read_private_bytes(path, label).decode("utf-8")
    except UnicodeError as exc:
        raise AppError(f"cannot decode {label} {path}: {exc}") from exc


def _require_writer_exclusion(
    writer_exclusion: OperationLock | None, path: Path, parent_descriptor: int
) -> None:
    if writer_exclusion is None or not writer_exclusion.held:
        raise AppError("conditional mutation requires a held writer exclusion lock")
    if not writer_exclusion.protects(path, parent_descriptor):
        raise AppError(f"writer exclusion does not protect destination: {path}")


def _require_version(parent_descriptor: int, path: Path, label: str, expected: FileVersion) -> None:
    if _version_at(parent_descriptor, path, label) != expected:
        raise ConcurrentModificationError(f"{label} changed before mutation: {path}")


def conditional_write_bytes(
    path: Path,
    content: bytes,
    expected: FileVersion,
    label: str,
    *,
    writer_exclusion: OperationLock | None,
    _event_log: list[str] | None = None,
) -> FileVersion:
    parent_descriptor = _open_private_parent(path, label)
    temporary_name: str | None = None
    try:
        _require_writer_exclusion(writer_exclusion, path, parent_descriptor)
        _require_version(parent_descriptor, path, label, expected)
        if expected.sha256 == hashlib.sha256(content).hexdigest():
            return expected
        temporary_name = f".{path.name}.{secrets.token_hex(8)}"
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                if _event_log is not None:
                    _event_log.append("temp-fsync")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        _require_version(parent_descriptor, path, label, expected)
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        temporary_name = None
        os.fsync(parent_descriptor)
        return _version_at(parent_descriptor, path, label)
    except AppError:
        raise
    except OSError as exc:
        raise AppError(f"cannot atomically write {label} {path}: {exc}") from exc
    finally:
        if temporary_name is not None:
            with suppress(OSError):
                os.unlink(temporary_name, dir_fd=parent_descriptor)
        os.close(parent_descriptor)


def conditional_delete(
    path: Path,
    expected: FileVersion,
    label: str,
    *,
    writer_exclusion: OperationLock | None,
) -> None:
    parent_descriptor = _open_private_parent(path, label)
    try:
        _require_writer_exclusion(writer_exclusion, path, parent_descriptor)
        _require_version(parent_descriptor, path, label, expected)
        if not expected.exists:
            return
        os.unlink(path.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except AppError:
        raise
    except OSError as exc:
        raise AppError(f"cannot delete {label} {path}: {exc}") from exc
    finally:
        os.close(parent_descriptor)


def _create_private_parents(path: Path, label: str) -> None:
    descriptor = open_private_parent(path, label, create=True)
    os.close(descriptor)


def write_new_private_file(path: Path, text: str) -> None:
    if not path.parent.exists():
        _create_private_parents(path, "registry")
    parent_descriptor = _open_private_parent(path, "registry")
    created = False
    file_descriptor: int | None = None
    try:
        try:
            os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise AppError(f"cannot inspect registry {path}: {exc}") from exc
        else:
            raise AppError(f"refusing to overwrite existing registry: {path}")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_descriptor)
        except OSError as exc:
            raise AppError(f"cannot create registry {path}: {exc}") from exc
        created = True
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                file_descriptor = None
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(parent_descriptor)
        except (OSError, UnicodeError) as exc:
            raise AppError(f"cannot write registry {path}: {exc}") from exc
    except AppError:
        if created:
            try:
                os.unlink(path.name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
            except OSError:
                pass
        raise
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        os.close(parent_descriptor)


def conditional_write_private_text(path: Path, text: str, label: str) -> None:
    """Conditionally replace a trusted private text file under its operation lock."""
    encoded = text.encode("utf-8")
    if not path.parent.exists():
        parent_descriptor = open_private_parent(path, label, create=True)
        os.close(parent_descriptor)
    with operation_lock(path, label) as lock:
        expected = inspect_private_file(path, label)
        conditional_write_bytes(path, encoded, expected, label, writer_exclusion=lock)


def atomic_write_json(path: Path, value: Any) -> None:
    """Write private ModFig-owned JSON under a kernel-held operation lock."""
    require_secure_io()
    if not platform_capabilities().operation_lock:
        raise CapabilityUnavailableError("operation lock is unavailable on this platform")
    serialized = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    serialized_sha256 = hashlib.sha256(serialized).hexdigest()
    try:
        current = inspect_private_file(path, "destination")
    except PrivateParentMissingError:
        current = None
    if current is not None and current.sha256 == serialized_sha256:
        return
    if current is None:
        parent_descriptor = open_private_parent(path, "destination", create=True)
        os.close(parent_descriptor)
    with operation_lock(path, "modfig-metadata") as lock:
        expected = inspect_private_file(path, "destination")
        if expected.sha256 == serialized_sha256:
            return
        conditional_write_bytes(
            path,
            serialized,
            expected,
            "destination",
            writer_exclusion=lock,
        )
