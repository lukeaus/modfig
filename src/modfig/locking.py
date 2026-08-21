from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .errors import AppError
from .platform import CapabilityUnavailableError, open_private_parent, platform_capabilities


class LockContentionError(AppError):
    """Another process holds the operation lock."""


_LOCK_FACTORY = object()


class OperationLock:
    __slots__ = (
        "_descriptor",
        "_destination_identity",
        "_held",
        "_lock_basename",
        "_lock_identity",
        "_parent_descriptor",
        "_parent_identity",
        "_target",
    )

    def __init__(
        self,
        destination_identity: str,
        target: str,
        descriptor: int,
        lock_identity: tuple[int, int],
        lock_basename: str,
        parent_descriptor: int,
        parent_identity: tuple[int, int],
        *,
        _factory: object,
    ) -> None:
        if _factory is not _LOCK_FACTORY:
            raise TypeError("OperationLock instances are created by operation_lock()")
        self._destination_identity = destination_identity
        self._target = target
        self._descriptor = descriptor
        self._lock_identity = lock_identity
        self._lock_basename = lock_basename
        self._parent_descriptor = parent_descriptor
        self._parent_identity = parent_identity
        self._held = True

    @property
    def destination_identity(self) -> str:
        return self._destination_identity

    @property
    def lock_identity(self) -> tuple[int, int]:
        return self._lock_identity

    @property
    def target(self) -> str:
        return self._target

    @property
    def held(self) -> bool:
        if not self._held:
            return False
        try:
            status = os.fstat(self._descriptor)
        except OSError:
            return False
        return (status.st_dev, status.st_ino) == self._lock_identity

    def protects(self, destination: Path, mutation_parent_descriptor: int | None = None) -> bool:
        if not self.held or self._destination_identity != str(destination.absolute()):
            return False
        try:
            status = os.stat(
                self._lock_basename,
                dir_fd=self._parent_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            return False
        if not stat.S_ISREG(status.st_mode):
            return False
        if status.st_uid != os.getuid():
            return False
        if status.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            return False
        if (status.st_dev, status.st_ino) != self._lock_identity:
            return False
        if mutation_parent_descriptor is None:
            return True
        try:
            mutation_parent = os.fstat(mutation_parent_descriptor)
        except OSError:
            return False
        return (mutation_parent.st_dev, mutation_parent.st_ino) == self._parent_identity

    def __copy__(self) -> OperationLock:
        raise TypeError("OperationLock cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> OperationLock:
        raise TypeError("OperationLock cannot be copied")

    def _release(self) -> None:
        self._held = False


@contextmanager
def operation_lock(
    destination: Path, target: str, *, blocking: bool = False
) -> Iterator[OperationLock]:
    if not platform_capabilities().operation_lock:
        raise CapabilityUnavailableError(
            "operation lock is unavailable: native Windows locking is not proven"
        )

    import fcntl

    destination_identity = str(destination.absolute())
    lock_path = destination.parent / f".{destination.name}.modfig.lock"
    lock_basename = lock_path.name
    parent_descriptor = open_private_parent(lock_path, "operation lock", create=True)
    descriptor: int | None = None
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_basename, flags, 0o600, dir_fd=parent_descriptor)
        except OSError as exc:
            raise AppError(f"cannot open operation lock {lock_path}: {exc}") from exc
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise AppError(f"operation lock must be a regular file: {lock_path}")
        if status.st_uid != os.getuid():
            raise AppError(f"operation lock must be owned by the current user: {lock_path}")
        if status.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise AppError(f"operation lock must be owner-only: {lock_path}")
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(descriptor, flags)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise LockContentionError(
                    f"operation is already in progress for {target} at {destination_identity}"
                ) from exc
            raise AppError(f"cannot acquire operation lock {lock_path}: {exc}") from exc
        parent_status = os.fstat(parent_descriptor)
        lock = OperationLock(
            destination_identity,
            target,
            descriptor,
            (status.st_dev, status.st_ino),
            lock_basename,
            parent_descriptor,
            (parent_status.st_dev, parent_status.st_ino),
            _factory=_LOCK_FACTORY,
        )
        try:
            yield lock
        finally:
            lock._release()
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except AppError:
        raise
    except OSError as exc:
        raise AppError(f"cannot use operation lock {lock_path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


@contextmanager
def operation_locks(
    anchor: Path, destinations: Iterator[Path] | tuple[Path, ...], label: str
) -> Iterator[dict[Path, OperationLock]]:
    anchor = anchor.absolute()
    remaining = sorted({path.absolute() for path in destinations} - {anchor}, key=str)
    with ExitStack() as stack:
        paths = (anchor, *remaining)
        yield {path: stack.enter_context(operation_lock(path, label)) for path in paths}
