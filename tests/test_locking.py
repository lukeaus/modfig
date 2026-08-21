from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest

from modfig.errors import AppError
from modfig.locking import LockContentionError, OperationLock, operation_lock
from modfig.platform import CapabilityUnavailableError
from modfig.storage import conditional_write_bytes, inspect_private_file

POSIX_SECURE_IO = pytest.mark.skipif(os.name == "nt", reason="requires native POSIX secure I/O")


def test_operation_lock_token_cannot_be_constructed_by_caller() -> None:
    with pytest.raises(TypeError):
        OperationLock("settings:1", "factory", 1, (1, 1))


@POSIX_SECURE_IO
def test_operation_lock_derives_canonical_lock_from_destination(tmp_path: Path) -> None:
    destination = tmp_path / "settings.json"
    unrelated = tmp_path / "other.json"
    canonical_lock = tmp_path / ".settings.json.modfig.lock"

    with operation_lock(destination, "factory") as lock:
        assert canonical_lock.exists()
        assert canonical_lock.stat().st_mode & 0o777 == 0o600
        assert lock.protects(destination)
        assert not lock.protects(unrelated)


@POSIX_SECURE_IO
def test_operation_lock_token_cannot_be_copied(tmp_path: Path) -> None:
    path = tmp_path / "operation.lock"
    with operation_lock(path, "factory") as lock:
        with pytest.raises(TypeError):
            copy.copy(lock)
        with pytest.raises(TypeError):
            copy.deepcopy(lock)


def test_operation_lock_acquires_contends_releases_and_retains_binding(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("native POSIX flock contract")
    destination = tmp_path / "settings.json"
    lock_path = tmp_path / ".settings.json.modfig.lock"

    with operation_lock(destination, "factory") as acquired:
        assert acquired.target == "factory"
        assert acquired.destination_identity == str(destination.absolute())
        assert acquired.lock_identity == (lock_path.stat().st_dev, lock_path.stat().st_ino)
        with (
            pytest.raises(LockContentionError, match="operation is already in progress"),
            operation_lock(destination, "factory"),
        ):
            pass

    with operation_lock(destination, "factory") as reacquired:
        assert reacquired.protects(destination)
    assert lock_path.exists()


def test_operation_lock_reuses_preexisting_unlocked_file(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("native POSIX flock contract")
    destination = tmp_path / "settings.json"
    lock_path = tmp_path / ".settings.json.modfig.lock"
    lock_path.write_bytes(b"")
    lock_path.chmod(0o600)

    with operation_lock(destination, "vscode"):
        pass

    assert lock_path.exists()


def test_operation_lock_rejects_existing_insecure_mode_without_chmod(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("native POSIX flock contract")
    destination = tmp_path / "settings.json"
    lock_path = tmp_path / ".settings.json.modfig.lock"
    lock_path.write_bytes(b"")
    lock_path.chmod(0o644)

    with (
        pytest.raises(AppError, match="owner-only"),
        operation_lock(destination, "factory"),
    ):
        pass

    assert lock_path.stat().st_mode & 0o777 == 0o644


def test_operation_lock_rejects_unsafe_intermediate_ancestor(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("native POSIX flock contract")
    unsafe = tmp_path / "unsafe"
    parent = unsafe / "private"
    parent.mkdir(parents=True)
    unsafe.chmod(0o777)
    parent.chmod(0o700)
    path = parent / "operation.lock"

    with (
        pytest.raises(AppError, match="ancestor.*writable|unsafe ancestor"),
        operation_lock(path, "factory"),
    ):
        pass

    assert not path.exists()


def test_operation_lock_rejects_symlinked_parent_without_redirect(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("native POSIX flock contract")
    external = tmp_path / "external"
    external.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(external, target_is_directory=True)
    path = linked / "operation.lock"

    with (
        pytest.raises(AppError, match="symlink|unsafe"),
        operation_lock(path, "factory"),
    ):
        pass

    assert list(external.iterdir()) == []


def test_operation_lock_rejects_symlink_file(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("native POSIX flock contract")
    target = tmp_path / "target"
    target.write_bytes(b"")
    destination = tmp_path / "settings.json"
    lock_path = tmp_path / ".settings.json.modfig.lock"
    lock_path.symlink_to(target)

    with (
        pytest.raises(AppError, match="regular|open"),
        operation_lock(destination, "factory"),
    ):
        pass


def test_operation_lock_replacement_canonical_lock_does_not_protect_destination(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("native POSIX flock contract")
    destination = tmp_path / "settings.json"
    destination.write_bytes(b"before")
    destination.chmod(0o600)
    lock_path = tmp_path / ".settings.json.modfig.lock"
    version = inspect_private_file(destination, "destination")

    with operation_lock(destination, "factory") as lock:
        # Same parent owner unlinks and recreates the canonical lock inode
        # while the original fd still holds flock on the unlinked inode.
        os.unlink(lock_path)
        replacement_fd = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.close(replacement_fd)

        assert not lock.protects(destination)
        with pytest.raises(AppError, match="does not protect destination"):
            conditional_write_bytes(
                destination,
                b"after",
                version,
                "destination",
                writer_exclusion=lock,
            )

    assert destination.read_bytes() == b"before"


def test_windows_operation_lock_fails_closed_before_file_creation(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "operation.lock"
    monkeypatch.setattr("modfig.platform._os_name", lambda: "nt")

    with (
        pytest.raises(CapabilityUnavailableError, match="operation lock"),
        operation_lock(path, "chatgpt"),
    ):
        pass

    assert not path.exists()


def test_operation_locks_acquires_anchor_then_canonical_deduped_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from modfig.locking import operation_locks

    anchor = tmp_path / "manifest.json"
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    events: list[tuple[str, str]] = []

    @contextmanager
    def fake_lock(path: Path, label: str):
        events.append(("acquire", str(path.absolute())))
        yield path
        events.append(("release", str(path.absolute())))

    monkeypatch.setattr("modfig.locking.operation_lock", fake_lock)
    with operation_locks(anchor, (second, first, second, anchor), "invocation") as locks:
        assert list(locks) == [anchor.absolute(), first.absolute(), second.absolute()]

    assert events == [
        ("acquire", str(anchor.absolute())),
        ("acquire", str(first.absolute())),
        ("acquire", str(second.absolute())),
        ("release", str(second.absolute())),
        ("release", str(first.absolute())),
        ("release", str(anchor.absolute())),
    ]
