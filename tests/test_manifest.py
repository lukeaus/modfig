from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath

import pytest

from modfig.components import ExtensionComponent
from modfig.errors import AppError
from modfig.locking import operation_lock
from modfig.manifest import (
    AdapterProvenance,
    ClientOwnership,
    ComponentOwnership,
    OwnershipManifest,
    load_ownership_manifest,
    load_ownership_manifest_snapshot,
    ownership_manifest_owned_components,
    parse_ownership_manifest_bytes,
    save_ownership_manifest,
)
from modfig.storage import ConcurrentModificationError

POSIX_SECURE_IO = pytest.mark.skipif(os.name == "nt", reason="requires native POSIX secure I/O")

_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _ownership_manifest() -> OwnershipManifest:
    return OwnershipManifest(
        registry_sha256=_SHA_A,
        selected_targets_sha256=_SHA_B,
        clients={
            "factory": ClientOwnership(
                components=(
                    ComponentOwnership(
                        component="core",
                        adapter=AdapterProvenance(
                            adapter_id="io.modfig.factory",
                            distribution="builtin",
                            version="0.1",
                        ),
                        grant_id="factory-settings",
                        artifact_path=PurePosixPath("settings.json"),
                        preimage_sha256=None,
                        written_sha256=_SHA_A,
                        ownership={"droidIds": ["reviewer"], "favoriteIds": [], "modelIds": []},
                    ),
                    ComponentOwnership(
                        component=ExtensionComponent("oh-my-droid"),
                        adapter=AdapterProvenance(
                            adapter_id="io.example.factory.oh-my-droid",
                            distribution="example-oh-my-droid",
                        ),
                        grant_id="droid-settings",
                        artifact_path=PurePosixPath("droids/settings.json"),
                        preimage_sha256=_SHA_B,
                        written_sha256=_SHA_A,
                        ownership={"plugin": "configured"},
                    ),
                ),
            ),
            "cursor": ClientOwnership(
                components=(
                    ComponentOwnership(
                        component="core",
                        adapter=AdapterProvenance(
                            adapter_id="io.example.cursor",
                            distribution="example-cursor",
                        ),
                        grant_id="cursor-settings",
                        artifact_path=PurePosixPath("settings.json"),
                        preimage_sha256=_SHA_B,
                        written_sha256=_SHA_A,
                        ownership={},
                    ),
                ),
            ),
        },
    )


def _save_ownership(path: Path, manifest: OwnershipManifest) -> None:
    snapshot = load_ownership_manifest_snapshot(path)
    with operation_lock(path, "manifest") as lock:
        save_ownership_manifest(path, manifest, snapshot, lock)


@POSIX_SECURE_IO
def test_ownership_manifest_missing_file_is_empty_v3(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    assert load_ownership_manifest(path) == OwnershipManifest()
    snapshot = load_ownership_manifest_snapshot(path)
    assert snapshot.manifest == OwnershipManifest()
    assert snapshot.serialized is None
    assert snapshot.sha256 is None
    assert not snapshot._version.exists


@POSIX_SECURE_IO
def test_ownership_manifest_missing_parent_is_empty_v3(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "missing" / "manifest.json"
    assert load_ownership_manifest(path) == OwnershipManifest()
    assert not (tmp_path / "nested").exists()


@POSIX_SECURE_IO
def test_ownership_manifest_missing_parent_snapshot_supports_first_conditional_save(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nested" / "missing" / "manifest.json"
    manifest = _ownership_manifest()
    snapshot = load_ownership_manifest_snapshot(path)

    with operation_lock(path, "manifest") as lock:
        save_ownership_manifest(path, manifest, snapshot, lock)

    assert load_ownership_manifest(path) == manifest


@POSIX_SECURE_IO
def test_ownership_manifest_round_trip_core_and_extension(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest = _ownership_manifest()

    _save_ownership(path, manifest)
    first = path.read_bytes()
    loaded = load_ownership_manifest(path)
    _save_ownership(path, loaded)

    assert loaded == manifest
    assert path.read_bytes() == first
    assert json.loads(first)["manifestVersion"] == 3
    assert path.stat().st_mode & 0o777 == 0o600
    # Opaque ownership, adapter provenance, grant id, artifact path, preimage,
    # and written hash all round-trip through JSON-safe deterministic bytes.
    factory_core = loaded.clients["factory"].components[0]
    assert factory_core.component == "core"
    assert factory_core.adapter == AdapterProvenance("io.modfig.factory", "builtin", "0.1")
    assert factory_core.grant_id == "factory-settings"
    assert factory_core.artifact_path == PurePosixPath("settings.json")
    assert factory_core.preimage_sha256 is None
    assert factory_core.written_sha256 == _SHA_A
    assert dict(factory_core.ownership) == {
        "droidIds": ("reviewer",),
        "favoriteIds": (),
        "modelIds": (),
    }
    factory_ext = loaded.clients["factory"].components[1]
    assert factory_ext.component == ExtensionComponent("oh-my-droid")
    assert factory_ext.adapter == AdapterProvenance(
        "io.example.factory.oh-my-droid", "example-oh-my-droid"
    )
    assert factory_ext.preimage_sha256 == _SHA_B
    cursor_core = loaded.clients["cursor"].components[0]
    assert cursor_core.component == "core"
    assert cursor_core.adapter.distribution == "example-cursor"


@POSIX_SECURE_IO
def test_ownership_manifest_conditional_save_rejects_stale_version(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    _save_ownership(path, _ownership_manifest())
    stale = load_ownership_manifest_snapshot(path)
    replacement = OwnershipManifest(registry_sha256=_SHA_B, selected_targets_sha256=_SHA_A)
    _save_ownership(path, replacement)

    with (
        operation_lock(path, "manifest") as lock,
        pytest.raises(ConcurrentModificationError, match="changed before mutation"),
    ):
        save_ownership_manifest(path, stale.manifest, stale, lock)

    assert load_ownership_manifest(path) == replacement


@POSIX_SECURE_IO
def test_ownership_manifest_update_preserves_sibling_client(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest = _ownership_manifest()
    _save_ownership(path, manifest)
    loaded = load_ownership_manifest(path)
    factory = loaded.clients["factory"]
    updated_factory = ClientOwnership(
        components=(
            ComponentOwnership(
                component="core",
                adapter=factory.components[0].adapter,
                grant_id=factory.components[0].grant_id,
                artifact_path=factory.components[0].artifact_path,
                preimage_sha256=factory.components[0].preimage_sha256,
                written_sha256=_SHA_B,
                ownership=factory.components[0].ownership,
            ),
        )
    )
    updated = OwnershipManifest(
        registry_sha256=loaded.registry_sha256,
        selected_targets_sha256=loaded.selected_targets_sha256,
        clients={**loaded.clients, "factory": updated_factory},
    )

    _save_ownership(path, updated)
    reloaded = load_ownership_manifest(path)

    assert reloaded.clients["cursor"] == manifest.clients["cursor"]


@POSIX_SECURE_IO
def test_ownership_manifest_owned_components_selects_v3_components(tmp_path: Path) -> None:
    manifest = _ownership_manifest()
    owned = ownership_manifest_owned_components(manifest)
    assert owned["factory"] == ("core", ExtensionComponent("oh-my-droid"))
    assert owned["cursor"] == ("core",)


def test_parse_ownership_manifest_bytes_rejects_v1_v2_and_unknown_version() -> None:
    with pytest.raises(AppError, match="current manifest v3"):
        parse_ownership_manifest_bytes(b'{"manifestVersion":1}\n')
    with pytest.raises(AppError, match="current manifest v3"):
        parse_ownership_manifest_bytes(b'{"manifestVersion":2,"targets":{}}\n')
    with pytest.raises(AppError, match="current manifest v3"):
        parse_ownership_manifest_bytes(b'{"manifestVersion":99}\n')


@pytest.mark.parametrize("version", [None, 3, {}, []])
def test_parse_ownership_manifest_rejects_invalid_adapter_provenance_version(
    version: object,
) -> None:
    raw = {
        "manifestVersion": 3,
        "registrySha256": _SHA_A,
        "selectedTargetsSha256": _SHA_B,
        "clients": {
            "factory": {
                "components": [
                    {
                        "component": "core",
                        "adapter": {
                            "adapterId": "io.modfig.factory",
                            "distribution": "builtin",
                            "version": version,
                        },
                        "grantId": "factory-settings",
                        "artifactPath": "settings.json",
                        "writtenSha256": _SHA_A,
                        "ownership": {},
                    }
                ]
            }
        },
    }
    with pytest.raises(AppError, match="version"):
        parse_ownership_manifest_bytes(json.dumps(raw).encode())


@POSIX_SECURE_IO
def test_ownership_manifest_load_rejects_nonempty_legacy_before_writes(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"manifestVersion": 2, "targets": {}}) + "\n", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(AppError, match="current manifest v3"):
        load_ownership_manifest(path)


@POSIX_SECURE_IO
def test_ownership_manifest_load_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{not json", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(AppError, match="manifest|JSON"):
        load_ownership_manifest(path)


@POSIX_SECURE_IO
def test_ownership_manifest_load_rejects_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(AppError, match="manifest|JSON"):
        load_ownership_manifest(path)


def test_ownership_manifest_rejects_nonfinite_json_numbers() -> None:
    document = b'{"manifestVersion": 3, "ignored": NaN, "clients": {}}'
    with pytest.raises(AppError, match="manifest|JSON|number"):
        parse_ownership_manifest_bytes(document)


def test_ownership_manifest_rejects_absolute_or_escaping_artifact_path() -> None:
    with pytest.raises(AppError, match="artifact path"):
        ComponentOwnership(
            component="core",
            adapter=AdapterProvenance("io.modfig.factory", "builtin"),
            grant_id="factory-settings",
            artifact_path=PurePosixPath("/etc/passwd"),
            preimage_sha256=None,
            written_sha256=_SHA_A,
            ownership={},
        )
    with pytest.raises(AppError, match="artifact path"):
        ComponentOwnership(
            component="core",
            adapter=AdapterProvenance("io.modfig.factory", "builtin"),
            grant_id="factory-settings",
            artifact_path=PurePosixPath("../escape.json"),
            preimage_sha256=None,
            written_sha256=_SHA_A,
            ownership={},
        )


def test_component_ownership_default_ownership_is_an_empty_mapping() -> None:
    record = ComponentOwnership(
        component="core",
        adapter=AdapterProvenance("io.modfig.factory", "builtin"),
        grant_id="factory-settings",
        artifact_path=PurePosixPath("settings.json"),
        preimage_sha256=None,
        written_sha256=_SHA_A,
    )

    assert dict(record.ownership) == {}


def test_ownership_manifest_freezes_nested_ownership() -> None:
    record = ComponentOwnership(
        component="core",
        adapter=AdapterProvenance("io.modfig.factory", "builtin"),
        grant_id="factory-settings",
        artifact_path=PurePosixPath("settings.json"),
        preimage_sha256=None,
        written_sha256=_SHA_A,
        ownership={"nested": {"items": ["original"]}},
    )

    with pytest.raises(TypeError):
        record.ownership["nested"]["items"] += ("mutated",)  # type: ignore[index,operator]
    with pytest.raises(AttributeError):
        record.ownership["nested"]["items"].append("mutated")  # type: ignore[index,union-attr]
    assert record.ownership["nested"]["items"] == ("original",)  # type: ignore[index]


def test_ownership_manifest_rejects_non_json_safe_ownership() -> None:
    with pytest.raises(AppError, match="JSON-safe"):
        ComponentOwnership(
            component="core",
            adapter=AdapterProvenance("io.modfig.factory", "builtin"),
            grant_id="factory-settings",
            artifact_path=PurePosixPath("settings.json"),
            preimage_sha256=None,
            written_sha256=_SHA_A,
            ownership={"bad": object()},
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_ownership_manifest_rejects_nonfinite_opaque_ownership(value: float) -> None:
    with pytest.raises(AppError, match="JSON-safe"):
        ComponentOwnership(
            component="core",
            adapter=AdapterProvenance("io.modfig.factory", "builtin"),
            grant_id="factory-settings",
            artifact_path=PurePosixPath("settings.json"),
            preimage_sha256=None,
            written_sha256=_SHA_A,
            ownership={"bad": value},
        )


@POSIX_SECURE_IO
def test_ownership_manifest_missing_under_symlinked_ancestor_is_rejected(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AppError, match="symlink|unsafe"):
        load_ownership_manifest(linked / "manifest.json")


@POSIX_SECURE_IO
def test_ownership_manifest_load_rejects_symlink_and_non_private_mode(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    target.chmod(0o600)
    symlink = tmp_path / "manifest.json"
    symlink.symlink_to(target)

    with pytest.raises(AppError, match="symlink"):
        load_ownership_manifest(symlink)

    target.chmod(0o644)
    with pytest.raises(AppError, match="owner-only"):
        load_ownership_manifest(target)
