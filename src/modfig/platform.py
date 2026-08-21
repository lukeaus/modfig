from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import AppError


class CapabilityUnavailableError(AppError):
    """A required native security guarantee has not been proven on this platform."""


class PrivateParentMissingError(AppError):
    """A securely traversed parent path is genuinely missing."""


@dataclass(frozen=True)
class PlatformCapabilities:
    secure_io: bool
    operation_lock: bool


def _os_name() -> str:
    return os.name


def _has_flock() -> bool:
    try:
        import fcntl
    except ImportError:
        return False
    return fcntl is not None


def _has_secure_io_primitives() -> bool:
    return hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")


def platform_capabilities() -> PlatformCapabilities:
    posix = _os_name() == "posix"
    secure_io = posix and _has_secure_io_primitives()
    operation_lock = secure_io and _has_flock()
    return PlatformCapabilities(secure_io=secure_io, operation_lock=operation_lock)


def require_secure_io() -> PlatformCapabilities:
    capabilities = platform_capabilities()
    if not capabilities.secure_io:
        raise CapabilityUnavailableError(
            "secure I/O is unavailable: native Windows confinement is not proven"
        )
    return capabilities


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _validate_ancestor(status: os.stat_result, label: str, component: str) -> None:
    if not stat.S_ISDIR(status.st_mode):
        raise AppError(f"{label} ancestor is not a directory: {component}")
    writable = bool(status.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
    trusted_sticky_root = status.st_uid == 0 and bool(status.st_mode & stat.S_ISVTX)
    if status.st_uid not in (0, os.getuid()):
        raise AppError(f"{label} ancestor has untrusted owner: {component}")
    if status.st_uid == os.getuid() and writable:
        raise AppError(f"{label} ancestor must not be writable by group or others: {component}")
    if status.st_uid != os.getuid() and writable and not trusted_sticky_root:
        raise AppError(f"{label} has unsafe ancestor permissions: {component}")


def _validate_final_parent(status: os.stat_result, label: str, path: Path) -> None:
    if not stat.S_ISDIR(status.st_mode):
        raise AppError(f"{label} parent is not a directory: {path.parent}")
    if status.st_uid != os.getuid():
        raise AppError(f"{label} parent must be owned by the current user: {path.parent}")
    if status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise AppError(f"{label} parent must not be writable by group or others: {path.parent}")


def open_private_parent(path: Path, label: str, *, create: bool = False) -> int:
    require_secure_io()
    absolute = path.absolute()
    parts = absolute.parent.parts
    flags = _directory_flags()
    descriptor = os.open(Path(parts[0]), flags)
    try:
        initial_status = os.fstat(descriptor)
        if len(parts) == 1:
            _validate_final_parent(initial_status, label, absolute)
        else:
            _validate_ancestor(initial_status, label, parts[0])
        for index, part in enumerate(parts[1:], start=1):
            final = index == len(parts) - 1
            try:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise PrivateParentMissingError(
                        f"cannot open {label} parent {absolute.parent}: missing"
                    ) from None
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    next_descriptor = os.open(part, flags, dir_fd=descriptor)
                except OSError as exc:
                    raise AppError(f"cannot create {label} parent {part}: {exc}") from exc
            except OSError as exc:
                raise AppError(
                    f"cannot open {label} parent ancestor {part}: symlink or unsafe: {exc}"
                ) from exc
            try:
                status = os.fstat(next_descriptor)
                if final:
                    _validate_final_parent(status, label, absolute)
                else:
                    _validate_ancestor(status, label, part)
            except Exception:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise
