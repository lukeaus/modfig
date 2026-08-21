from __future__ import annotations

import os
from pathlib import Path

import pytest

from modfig.errors import AppError
from modfig.platform import (
    CapabilityUnavailableError,
    open_private_parent,
    platform_capabilities,
    require_secure_io,
)
from modfig.storage import atomic_write_json

POSIX_SECURE_IO = pytest.mark.skipif(os.name == "nt", reason="requires native POSIX secure I/O")


def test_posix_reports_secure_io_and_lock_with_unproven_key_context() -> None:
    if os.name == "nt":
        pytest.skip("POSIX capability contract")

    capabilities = platform_capabilities()

    assert capabilities.secure_io is True
    assert capabilities.operation_lock is True
    assert require_secure_io() == capabilities


@POSIX_SECURE_IO
def test_untrusted_other_user_owned_ancestor_is_rejected(tmp_path: Path, monkeypatch) -> None:
    ancestor = tmp_path / "other"
    parent = ancestor / "private"
    parent.mkdir(parents=True)
    ancestor.chmod(0o755)
    parent.chmod(0o700)
    original_fstat = os.fstat
    ancestor_identity = (ancestor.stat().st_dev, ancestor.stat().st_ino)

    def report_other_owner(descriptor: int) -> os.stat_result:
        status = original_fstat(descriptor)
        if (status.st_dev, status.st_ino) == ancestor_identity:
            values = list(status)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return status

    monkeypatch.setattr(os, "fstat", report_other_owner)

    with pytest.raises(AppError, match="untrusted owner"):
        open_private_parent(parent / "file", "destination")


@POSIX_SECURE_IO
def test_rejected_parent_component_closes_open_descriptor(tmp_path: Path, monkeypatch) -> None:
    unsafe = tmp_path / "unsafe"
    parent = unsafe / "private"
    parent.mkdir(parents=True)
    unsafe.chmod(0o777)
    parent.chmod(0o700)
    opened: list[int] = []
    closed: list[int] = []
    original_open = os.open
    original_close = os.close

    def record_open(*args: object, **kwargs: object) -> int:
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def record_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "close", record_close)

    with pytest.raises(AppError, match="ancestor.*writable"):
        open_private_parent(parent / "file", "destination")

    assert set(opened) <= set(closed)


def test_windows_dispatch_fails_closed_without_loading_runtime_apis(monkeypatch) -> None:
    monkeypatch.setattr("modfig.platform._os_name", lambda: "nt")

    capabilities = platform_capabilities()

    assert capabilities.secure_io is False
    assert capabilities.operation_lock is False
    with pytest.raises(CapabilityUnavailableError, match="secure I/O"):
        require_secure_io()


def test_atomic_write_json_fails_when_required_no_follow_primitive_missing(
    tmp_path: Path, monkeypatch
) -> None:
    # HIGH finding: required O_NOFOLLOW / O_DIRECTORY must not silently degrade
    # to 0 while secure_io is still reported capable.
    if os.name != "posix":
        pytest.skip("POSIX capability contract")
    path = tmp_path / "missing" / "nested" / "manifest.json"
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

    with pytest.raises(CapabilityUnavailableError):
        atomic_write_json(path, {"new": True})

    assert not (tmp_path / "missing").exists()


def test_open_private_parent_rejects_root_as_final_parent_for_non_root_user() -> None:
    # MEDIUM finding: when destination is a direct child of /, the traversal
    # loop is empty and the initial root descriptor is returned unvalidated.
    # Root must be validated as the final current-user-private parent.
    if os.name != "posix":
        pytest.skip("POSIX capability contract")
    if os.getuid() == 0:
        pytest.skip("root runner owns /, cannot assert non-current-user ownership")
    with pytest.raises(AppError, match="owned"):
        open_private_parent(Path("/modfig-root-validation-probe"), "probe")
