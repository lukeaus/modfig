from __future__ import annotations

import inspect
import os
import stat
from hashlib import sha256
from pathlib import Path

import pytest

from modfig.errors import AppError
from modfig.locking import operation_lock
from modfig.platform import (
    CapabilityUnavailableError,
    PlatformCapabilities,
)
from modfig.registry import RegistryValidationError, load_registry, load_registry_text
from modfig.storage import (
    ConcurrentModificationError,
    FileVersion,
    atomic_write_json,
    conditional_delete,
    conditional_write_bytes,
    inspect_private_file,
    read_private_bytes,
    read_private_text,
    resolve_config_path,
    write_new_private_file,
)

POSIX_SECURE_IO = pytest.mark.skipif(os.name == "nt", reason="requires native POSIX secure I/O")

VALID_REGISTRY = """specVersion: "0.1"
providers:
  example:
    name: Example
    targets: [factory]
    baseUrl: https://api.example.com/v1
    apiKey: env.EXAMPLE_KEY
    enabled: true
    models:
      example-model:
        displayName: Example Model
        contextWindow: 8192
        maxOutputTokens: 1024
        enabled: true
"""


@POSIX_SECURE_IO
def test_secure_read_rejects_group_writable_registry(tmp_path: Path) -> None:
    path = tmp_path / "modfig.yaml"
    path.write_text(VALID_REGISTRY, encoding="utf-8")
    path.chmod(0o660)

    with pytest.raises(AppError, match="writable by group"):
        read_private_text(path, "registry")


@POSIX_SECURE_IO
def test_secure_read_rejects_world_readable_registry(tmp_path: Path) -> None:
    path = tmp_path / "modfig.yaml"
    path.write_text(VALID_REGISTRY, encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(AppError, match="owner-only"):
        read_private_text(path, "registry")


@POSIX_SECURE_IO
def test_secure_read_allows_non_writable_0755_parent(tmp_path: Path) -> None:
    path = tmp_path / "modfig.yaml"
    path.write_text(VALID_REGISTRY, encoding="utf-8")
    path.chmod(0o600)
    tmp_path.chmod(0o755)

    assert read_private_text(path, "registry") == VALID_REGISTRY


@POSIX_SECURE_IO
def test_secure_read_rejects_symlinked_registry(tmp_path: Path) -> None:
    target = tmp_path / "target.yaml"
    target.write_text(VALID_REGISTRY, encoding="utf-8")
    path = tmp_path / "modfig.yaml"
    path.symlink_to(target)

    with pytest.raises(AppError, match="must not be a symlink"):
        read_private_text(path, "registry")


@POSIX_SECURE_IO
def test_secure_read_rejects_symlinked_grandparent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    parent = real / "private"
    parent.mkdir(parents=True)
    parent.chmod(0o700)
    path = parent / "modfig.yaml"
    path.write_text(VALID_REGISTRY, encoding="utf-8")
    path.chmod(0o600)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(AppError, match="symlink|unsafe"):
        read_private_text(linked / "private" / "modfig.yaml", "registry")


def test_atomic_write_fails_before_parent_creation_when_lock_capability_missing(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "missing" / "manifest.json"
    monkeypatch.setattr(
        "modfig.storage.platform_capabilities",
        lambda: PlatformCapabilities(
            secure_io=True,
            operation_lock=False,
        ),
    )

    with pytest.raises(CapabilityUnavailableError, match="operation lock"):
        atomic_write_json(path, {"new": True})

    assert not path.parent.exists()


def test_atomic_write_windows_fails_before_creating_any_parent(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "missing" / "nested" / "manifest.json"
    monkeypatch.setattr("modfig.platform._os_name", lambda: "nt")

    with pytest.raises(CapabilityUnavailableError, match="secure I/O"):
        atomic_write_json(path, {"new": True})

    assert not (tmp_path / "missing").exists()


def test_atomic_write_windows_secure_io_gate_precedes_parent_inspection(
    tmp_path: Path, monkeypatch
) -> None:
    def _must_not_reach_parent_inspection(path: Path, label: str) -> int:
        raise AssertionError(f"_open_private_parent reached under forced Windows: {path} ({label})")

    monkeypatch.setattr("modfig.storage._open_private_parent", _must_not_reach_parent_inspection)
    monkeypatch.setattr("modfig.platform._os_name", lambda: "nt")

    with pytest.raises(CapabilityUnavailableError, match="secure I/O"):
        atomic_write_json(tmp_path / "missing" / "nested" / "manifest.json", {"new": True})


@POSIX_SECURE_IO
def test_unsafe_user_owned_intermediate_rejects_read_inspect_and_write(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    parent = unsafe / "private"
    parent.mkdir(parents=True)
    unsafe.chmod(0o777)
    parent.chmod(0o700)
    path = parent / "settings.json"
    path.write_bytes(b"before")
    path.chmod(0o600)

    with pytest.raises(AppError, match="ancestor.*writable|unsafe ancestor"):
        read_private_bytes(path, "destination")
    with pytest.raises(AppError, match="ancestor.*writable|unsafe ancestor"):
        inspect_private_file(path, "destination")
    with pytest.raises(AppError, match="ancestor.*writable|unsafe ancestor"):
        atomic_write_json(parent / "new.json", {"new": True})

    assert path.read_bytes() == b"before"
    assert not (parent / "new.json").exists()


@POSIX_SECURE_IO
def test_conditional_write_rejects_lock_for_different_destination(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    second.write_bytes(b"before")
    second.chmod(0o600)
    version = inspect_private_file(second, "destination")

    with (
        operation_lock(first, "test") as lock,
        pytest.raises(AppError, match="does not protect destination"),
    ):
        conditional_write_bytes(second, b"after", version, "destination", writer_exclusion=lock)

    assert second.read_bytes() == b"before"


def test_conditional_mutations_expose_no_boolean_writer_bypass() -> None:
    write_parameters = inspect.signature(conditional_write_bytes).parameters
    delete_parameters = inspect.signature(conditional_delete).parameters

    assert "external_writers_excluded" not in write_parameters
    assert "external_writers_excluded" not in delete_parameters
    assert "writer_exclusion" in write_parameters
    assert "writer_exclusion" in delete_parameters


@POSIX_SECURE_IO
def test_atomic_write_json_first_noop_creates_no_lock_or_parent_write(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_bytes(b'{\n  "same": true\n}\n')
    path.chmod(0o600)
    lock_path = tmp_path / ".manifest.json.modfig.lock"
    before = tmp_path.stat().st_mtime_ns

    atomic_write_json(path, {"same": True})

    assert not lock_path.exists()
    assert tmp_path.stat().st_mtime_ns == before


@POSIX_SECURE_IO
def test_atomic_write_json_noop_preserves_inode_and_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    atomic_write_json(path, {"same": True})
    before = path.stat()

    atomic_write_json(path, {"same": True})

    after = path.stat()
    assert (after.st_dev, after.st_ino, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_mtime_ns,
    )


@POSIX_SECURE_IO
def test_conditional_write_requires_currently_held_operation_lock(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b"before")
    path.chmod(0o600)
    version = inspect_private_file(path, "destination")
    with operation_lock(path, "factory") as lock:
        pass

    with pytest.raises(AppError, match="held writer exclusion"):
        conditional_write_bytes(path, b"after", version, "destination", writer_exclusion=lock)

    assert path.read_bytes() == b"before"


@POSIX_SECURE_IO
def test_conditional_delete_requires_currently_held_operation_lock(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b"before")
    path.chmod(0o600)
    version = inspect_private_file(path, "destination")
    with operation_lock(path, "factory") as lock:
        pass

    with pytest.raises(AppError, match="held writer exclusion"):
        conditional_delete(path, version, "destination", writer_exclusion=lock)

    assert path.read_bytes() == b"before"


def _conditional_write_with_lock(path: Path, content: bytes, version: FileVersion) -> FileVersion:
    with operation_lock(path, "test") as lock:
        return conditional_write_bytes(path, content, version, "destination", writer_exclusion=lock)


def _conditional_delete_with_lock(path: Path, version: FileVersion) -> None:
    with operation_lock(path, "test") as lock:
        conditional_delete(path, version, "destination", writer_exclusion=lock)


@POSIX_SECURE_IO
def test_file_version_contains_parent_identity_and_exact_byte_hash(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    content = b'{"setting":true}\n'
    path.write_bytes(content)
    path.chmod(0o600)

    version = inspect_private_file(path, "destination")

    assert version.exists is True
    assert version.parent_identity == (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
    assert version.leaf_identity == (path.stat().st_dev, path.stat().st_ino)
    assert version.size == len(content)
    assert version.sha256 == sha256(content).hexdigest()
    assert read_private_bytes(path, "destination") == content


@POSIX_SECURE_IO
def test_conditional_write_rejects_same_inode_same_size_content_edit(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b"before")
    path.chmod(0o600)
    version = inspect_private_file(path, "destination")

    path.write_bytes(b"change")
    path.chmod(0o600)

    with pytest.raises(ConcurrentModificationError, match="changed"):
        _conditional_write_with_lock(path, b"planned", version)
    assert path.read_bytes() == b"change"


@POSIX_SECURE_IO
def test_conditional_write_rejects_file_created_after_absent_inspection(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    version = inspect_private_file(path, "destination")
    assert version.exists is False

    path.write_bytes(b"concurrent")
    path.chmod(0o600)

    with pytest.raises(ConcurrentModificationError, match="changed"):
        _conditional_write_with_lock(path, b"planned", version)
    assert path.read_bytes() == b"concurrent"


@POSIX_SECURE_IO
def test_conditional_delete_preserves_changed_content(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b"before")
    path.chmod(0o600)
    version = inspect_private_file(path, "destination")
    path.write_bytes(b"change")
    path.chmod(0o600)

    with pytest.raises(ConcurrentModificationError, match="changed"):
        _conditional_delete_with_lock(path, version)
    assert path.read_bytes() == b"change"


@POSIX_SECURE_IO
def test_conditional_write_requires_writer_exclusion_before_temp_creation(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    version = inspect_private_file(path, "destination")

    with pytest.raises(AppError, match="writer exclusion"):
        conditional_write_bytes(
            path,
            b"planned",
            version,
            "destination",
            writer_exclusion=None,
        )

    assert list(tmp_path.iterdir()) == []


@POSIX_SECURE_IO
def test_conditional_write_replaces_unchanged_file_with_private_bytes(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b"before")
    path.chmod(0o600)
    version = inspect_private_file(path, "destination")

    result = _conditional_write_with_lock(path, b"after", version)

    assert path.read_bytes() == b"after"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert result == inspect_private_file(path, "destination")


@POSIX_SECURE_IO
def test_atomic_write_json_delegates_to_exact_byte_conditional_write(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "manifest.json"
    original = b'{"old":1}\n'
    path.write_bytes(original)
    path.chmod(0o600)
    calls: list[tuple[bytes, str, object]] = []

    def record_write(
        write_path: Path,
        content: bytes,
        expected: FileVersion,
        label: str,
        *,
        writer_exclusion: object,
    ) -> FileVersion:
        assert write_path == path
        assert expected.sha256 == sha256(original).hexdigest()
        calls.append((content, label, writer_exclusion))
        return expected

    monkeypatch.setattr("modfig.storage.conditional_write_bytes", record_write)

    atomic_write_json(path, {"planned": True})

    assert len(calls) == 1
    assert calls[0][:2] == (b'{\n  "planned": true\n}\n', "destination")
    assert calls[0][2] is not None


@POSIX_SECURE_IO
def test_new_config_creates_missing_nested_private_parents(tmp_path: Path) -> None:
    parent = tmp_path / "state" / "config"
    path = parent / "modfig.yaml"

    write_new_private_file(path, VALID_REGISTRY)

    assert read_private_text(path, "registry") == VALID_REGISTRY
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700


@POSIX_SECURE_IO
def test_new_config_refuses_symlinked_parent(tmp_path: Path) -> None:
    target_parent = tmp_path / "target"
    target_parent.mkdir()
    link_parent = tmp_path / "linked"
    link_parent.symlink_to(target_parent, target_is_directory=True)

    with pytest.raises(AppError, match="symlink"):
        write_new_private_file(link_parent / "modfig.yaml", VALID_REGISTRY)


def test_registry_rejects_credentials_in_base_url() -> None:
    text = VALID_REGISTRY.replace(
        "https://api.example.com/v1", "https://user:secret@example.com/v1"
    )

    with pytest.raises(RegistryValidationError, match="must not include credentials"):
        load_registry_text(text)


@POSIX_SECURE_IO
def test_new_config_cleans_up_created_file_after_write_failure(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "modfig.yaml"

    def fail_fsync(_: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(os, "fsync", fail_fsync)

    with pytest.raises(AppError, match="cannot write registry"):
        write_new_private_file(path, VALID_REGISTRY)

    assert not path.exists()


@POSIX_SECURE_IO
def test_new_config_cleans_up_after_unencodable_registry_text(tmp_path: Path) -> None:
    path = tmp_path / "modfig.yaml"

    with pytest.raises(AppError, match="cannot write registry"):
        write_new_private_file(path, "ok\ud800bad")

    assert not path.exists()


@POSIX_SECURE_IO
def test_new_config_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "modfig.yaml"

    write_new_private_file(path, VALID_REGISTRY)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_uid == os.getuid()


def test_conditional_mutations_reject_replaced_destination_parent_split_lock(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("native POSIX split-lock contract")
    # POSIX flock binds to the inode, not the directory entry. When
    # the original parent is renamed aside and a new parent is recreated at the
    # same textual location, the first token's flock still covers the original
    # (now relocated) lock inode while the textual destination now resolves
    # through a different parent directory. Bind the mutation parent identity to
    # the token's original parent identity so the first token cannot authorize a
    # mutation that opens the replacement parent. Bounds but does not eliminate
    # a same-UID race inside the original parent after revalidation.
    destination = tmp_path / "parent" / "settings.json"
    destination.parent.mkdir()
    destination.parent.chmod(0o700)
    destination.write_bytes(b'{"v":1}\n')
    destination.chmod(0o600)

    with operation_lock(destination, "first") as first_lock:
        old_parent = tmp_path / "old-parent"
        os.rename(destination.parent, old_parent)

        destination.parent.mkdir()
        destination.parent.chmod(0o700)
        new_bytes = b'{"v":2}\n'
        destination.write_bytes(new_bytes)
        destination.chmod(0o600)

        expected = inspect_private_file(destination, "destination")

        with operation_lock(destination, "second"):
            assert first_lock.protects(destination)

            with pytest.raises(AppError, match="does not protect destination"):
                conditional_write_bytes(
                    destination,
                    b'{"v":3}\n',
                    expected,
                    "destination",
                    writer_exclusion=first_lock,
                )
            assert destination.read_bytes() == new_bytes

            with pytest.raises(AppError, match="does not protect destination"):
                conditional_delete(
                    destination,
                    expected,
                    "destination",
                    writer_exclusion=first_lock,
                )
            assert destination.read_bytes() == new_bytes


# Config discovery tests (Task 1: safe XDG default with legacy fallback)


def _write_valid_legacy(home: Path) -> Path:
    legacy = home / ".modfig.yaml"
    legacy.write_text(VALID_REGISTRY, encoding="utf-8")
    legacy.chmod(0o600)
    return legacy


def _make_xdg_config_dir(xdg_root: Path) -> Path:
    config_dir = xdg_root / "modfig"
    config_dir.mkdir(parents=True)
    config_dir.chmod(0o700)
    return config_dir


def test_config_resolution_prefers_explicit_path(tmp_path: Path) -> None:
    chosen = tmp_path / "chosen.yaml"
    resolution = resolve_config_path(str(chosen), environ={}, home=tmp_path)
    assert resolution.path == chosen
    assert resolution.source == "explicit"


def test_config_resolution_prefers_environment_when_no_explicit(tmp_path: Path) -> None:
    env_path = tmp_path / "env.yaml"
    resolution = resolve_config_path(None, environ={"MODFIG_CONFIG": str(env_path)}, home=tmp_path)
    assert resolution.path == env_path
    assert resolution.source == "environment"


def test_default_resolution_uses_xdg_config_home(tmp_path: Path) -> None:
    resolution = resolve_config_path(
        None, environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg")}, home=tmp_path / "home"
    )
    assert resolution.path == tmp_path / "xdg" / "modfig" / "config.yaml"
    assert resolution.source == "xdg"


def test_default_resolution_uses_legacy_only_when_new_default_is_absent(tmp_path: Path) -> None:
    legacy = _write_valid_legacy(tmp_path)
    resolution = resolve_config_path(None, environ={}, home=tmp_path)
    assert resolution.path == legacy
    assert resolution.source == "legacy"


def test_default_resolution_uses_empty_xdg_config_home_as_home_config(tmp_path: Path) -> None:
    resolution = resolve_config_path(None, environ={"XDG_CONFIG_HOME": ""}, home=tmp_path)
    assert resolution.path == tmp_path / ".config" / "modfig" / "config.yaml"
    assert resolution.source == "xdg"


def test_default_resolution_uses_relative_xdg_config_home_as_home_config(tmp_path: Path) -> None:
    resolution = resolve_config_path(
        None, environ={"XDG_CONFIG_HOME": "relative/xdg"}, home=tmp_path
    )
    assert resolution.path == tmp_path / ".config" / "modfig" / "config.yaml"
    assert resolution.source == "xdg"


@POSIX_SECURE_IO
def test_default_resolution_prefers_new_default_over_legacy_when_both_exist(
    tmp_path: Path,
) -> None:
    config_dir = _make_xdg_config_dir(tmp_path / "xdg")
    new_default = config_dir / "config.yaml"
    new_default.write_text(VALID_REGISTRY, encoding="utf-8")
    new_default.chmod(0o600)
    _write_valid_legacy(tmp_path)
    resolution = resolve_config_path(
        None, environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg")}, home=tmp_path
    )
    assert resolution.path == new_default
    assert resolution.source == "xdg"


@POSIX_SECURE_IO
def test_explicit_config_path_has_no_fallback_when_missing(tmp_path: Path) -> None:
    _write_valid_legacy(tmp_path)
    missing = tmp_path / "absent.yaml"
    resolution = resolve_config_path(str(missing), environ={}, home=tmp_path)
    assert resolution.path == missing
    assert resolution.source == "explicit"
    with pytest.raises(AppError, match="does not exist"):
        load_registry(resolution.path)


@POSIX_SECURE_IO
def test_environment_config_path_has_no_fallback_when_missing(tmp_path: Path) -> None:
    _write_valid_legacy(tmp_path)
    missing = tmp_path / "absent.yaml"
    resolution = resolve_config_path(None, environ={"MODFIG_CONFIG": str(missing)}, home=tmp_path)
    assert resolution.path == missing
    assert resolution.source == "environment"
    with pytest.raises(AppError, match="does not exist"):
        load_registry(resolution.path)


@POSIX_SECURE_IO
def test_selected_xdg_config_rejects_symlink_without_legacy_fallback(tmp_path: Path) -> None:
    config_dir = _make_xdg_config_dir(tmp_path / "xdg")
    target = tmp_path / "target.yaml"
    target.write_text(VALID_REGISTRY, encoding="utf-8")
    target.chmod(0o600)
    symlinked = config_dir / "config.yaml"
    symlinked.symlink_to(target)
    _write_valid_legacy(tmp_path)
    resolution = resolve_config_path(
        None, environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg")}, home=tmp_path
    )
    assert resolution.source == "xdg"
    with pytest.raises(AppError, match="symlink"):
        load_registry(resolution.path)


@POSIX_SECURE_IO
def test_selected_xdg_config_rejects_non_regular_file_without_legacy_fallback(
    tmp_path: Path,
) -> None:
    config_dir = _make_xdg_config_dir(tmp_path / "xdg")
    non_regular = config_dir / "config.yaml"
    non_regular.mkdir()
    _write_valid_legacy(tmp_path)
    resolution = resolve_config_path(
        None, environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg")}, home=tmp_path
    )
    assert resolution.source == "xdg"
    with pytest.raises(AppError, match="not a regular file"):
        load_registry(resolution.path)


@POSIX_SECURE_IO
def test_selected_xdg_config_rejects_group_writable_file_without_legacy_fallback(
    tmp_path: Path,
) -> None:
    config_dir = _make_xdg_config_dir(tmp_path / "xdg")
    group_writable = config_dir / "config.yaml"
    group_writable.write_text(VALID_REGISTRY, encoding="utf-8")
    group_writable.chmod(0o660)
    _write_valid_legacy(tmp_path)
    resolution = resolve_config_path(
        None, environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg")}, home=tmp_path
    )
    assert resolution.source == "xdg"
    with pytest.raises(AppError, match="writable by group"):
        load_registry(resolution.path)


@POSIX_SECURE_IO
def test_selected_xdg_config_rejects_unsafe_parent_without_legacy_fallback(
    tmp_path: Path,
) -> None:
    xdg_root = tmp_path / "xdg"
    config_dir = _make_xdg_config_dir(xdg_root)
    config_file = config_dir / "config.yaml"
    config_file.write_text(VALID_REGISTRY, encoding="utf-8")
    config_file.chmod(0o600)
    xdg_root.chmod(0o777)
    _write_valid_legacy(tmp_path)
    resolution = resolve_config_path(
        None, environ={"XDG_CONFIG_HOME": str(xdg_root)}, home=tmp_path
    )
    assert resolution.source == "xdg"
    with pytest.raises(AppError, match="ancestor"):
        load_registry(resolution.path)
