"""Built-in renderer for Factory oh-my-droid personal overrides."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ....adapters import (
    AbsentDestination,
    AdapterContext,
    AdapterMetadata,
    AdapterPlanContext,
    AdapterPlanError,
    AdapterValidationContext,
    ArtifactIdentity,
    ArtifactPlan,
    ArtifactSnapshot,
    PlannedArtifact,
    PreflightDeclaration,
    ProspectiveWrite,
    RuntimeProof,
    SnapshotRequest,
)
from ....components import ExtensionComponent
from ....errors import AppError
from ....platform import PrivateParentMissingError, open_private_parent
from ....registry import InheritReference, ModelReference
from ....storage import FileVersion

_COMPONENT = ExtensionComponent("oh-my-droid")
_METADATA = AdapterMetadata("modfig.oh_my_droid", "factory", _COMPONENT)
PLUGIN_GRANT = "factory-plugins"
DROID_GRANT = "factory-droids"
PLUGIN_INVENTORY = PurePosixPath("installed_plugins.json")
PLUGIN_INVENTORY_DIR = PurePosixPath("installed_plugins")
_MAX_PLUGIN_ARTIFACT_BYTES = 16 * 1024 * 1024
_MAX_PLUGIN_METADATA_FILES = 256
_MAX_PLUGIN_METADATA_BYTES = 16 * 1024 * 1024
_DROID_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_MODEL_LINE_RE = re.compile(r"^model:\s")


def _plugin_root() -> Path:
    return (Path.home() / ".factory" / "plugins").absolute()


def _droids_root() -> Path:
    return (Path.home() / ".factory" / "droids").absolute()


def _read_plugin_bytes(path: Path) -> bytes:
    parent = open_private_parent(path, "Factory plugin file")
    descriptor = -1
    try:
        try:
            status = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            raise AppError(f"Factory plugin file does not exist: {path.name}") from None
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise AppError("Factory plugin file must be a regular non-symlink file")
        if status.st_uid != os.getuid() or status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise AppError("Factory plugin file has unsafe ownership or permissions")
        if status.st_size > _MAX_PLUGIN_ARTIFACT_BYTES:
            raise AppError("Factory plugin file exceeds 16 MiB")
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            status.st_dev,
            status.st_ino,
            status.st_size,
        ):
            raise AppError("Factory plugin file changed while opening")
        if opened.st_size > _MAX_PLUGIN_ARTIFACT_BYTES:
            raise AppError("Factory plugin file exceeds 16 MiB")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            content = handle.read(_MAX_PLUGIN_ARTIFACT_BYTES + 1)
        if len(content) > _MAX_PLUGIN_ARTIFACT_BYTES:
            raise AppError("Factory plugin file exceeds 16 MiB")
        if len(content) != status.st_size:
            raise AppError("Factory plugin file changed while reading")
        return content
    except OSError as exc:
        raise AppError("Factory plugin file could not be read") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def snapshot_plugin_file(path: Path) -> tuple[ArtifactSnapshot, FileVersion]:
    """Snapshot a plugin file while allowing non-world-writable cache files."""
    parent = open_private_parent(path, "Factory plugin file")
    descriptor = -1
    try:
        parent_status = os.fstat(parent)
        try:
            status = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return AbsentDestination(), FileVersion(
                (parent_status.st_dev, parent_status.st_ino), None, None, None
            )
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise AppError("Factory plugin file must be a regular non-symlink file")
        if status.st_uid != os.getuid() or status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise AppError("Factory plugin file has unsafe ownership or permissions")
        if status.st_size > _MAX_PLUGIN_ARTIFACT_BYTES:
            raise AppError("Factory plugin file exceeds 16 MiB")
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        opened = os.fstat(descriptor)
        if opened.st_size > _MAX_PLUGIN_ARTIFACT_BYTES:
            raise AppError("Factory plugin file exceeds 16 MiB")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            content = handle.read(_MAX_PLUGIN_ARTIFACT_BYTES + 1)
        if len(content) > _MAX_PLUGIN_ARTIFACT_BYTES:
            raise AppError("Factory plugin file exceeds 16 MiB")
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            status.st_dev,
            status.st_ino,
            status.st_size,
        ) or len(content) != status.st_size:
            raise AppError("Factory plugin file changed while snapshotting")
        version = FileVersion(
            (parent_status.st_dev, parent_status.st_ino),
            (status.st_dev, status.st_ino),
            status.st_size,
            hashlib.sha256(content).hexdigest(),
        )
        return content, version
    except OSError as exc:
        raise AppError("Factory plugin file could not be snapshotted") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _safe_droid_names(root: Path) -> tuple[str, ...]:
    if not root.exists():
        return ()
    parent = open_private_parent(root / ".modfig-scan", "Factory droids")
    os.close(parent)
    names: list[str] = []
    for path in root.iterdir():
        if path.suffix != ".md" or not _DROID_NAME_RE.fullmatch(path.stem):
            continue
        status = path.stat(follow_symlinks=False)
        if stat.S_ISREG(status.st_mode):
            names.append(path.stem)
    return tuple(sorted(names))


def snapshot_droid_file(path: Path) -> tuple[ArtifactSnapshot, FileVersion]:
    """Snapshot a user droid while permitting owner-readable legacy files."""
    parent = open_private_parent(path, "Factory droid")
    descriptor = -1
    try:
        parent_status = os.fstat(parent)
        try:
            status = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return AbsentDestination(), FileVersion(
                (parent_status.st_dev, parent_status.st_ino), None, None, None
            )
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise AppError("Factory droid must be a regular non-symlink file")
        if status.st_uid != os.getuid() or status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise AppError("Factory droid has unsafe ownership or permissions")
        if status.st_size > _MAX_PLUGIN_ARTIFACT_BYTES:
            raise AppError("Factory droid exceeds 16 MiB")
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        opened = os.fstat(descriptor)
        if opened.st_size > _MAX_PLUGIN_ARTIFACT_BYTES:
            raise AppError("Factory droid exceeds 16 MiB")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            content = handle.read(_MAX_PLUGIN_ARTIFACT_BYTES + 1)
        if len(content) > _MAX_PLUGIN_ARTIFACT_BYTES:
            raise AppError("Factory droid exceeds 16 MiB")
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            status.st_dev,
            status.st_ino,
            status.st_size,
        ) or len(content) != status.st_size:
            raise AppError("Factory droid changed while snapshotting")
        version = FileVersion(
            (parent_status.st_dev, parent_status.st_ino),
            (status.st_dev, status.st_ino),
            status.st_size,
            hashlib.sha256(content).hexdigest(),
        )
        return content, version
    except OSError as exc:
        raise AppError("Factory droid could not be snapshotted") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _inventory_artifacts(root: Path) -> tuple[PurePosixPath, ...]:
    metadata_root = root / PLUGIN_INVENTORY_DIR
    try:
        status = os.stat(metadata_root, follow_symlinks=False)
    except FileNotFoundError:
        status = None
    except OSError as exc:
        raise AppError("Factory plugin inventory directory could not be inspected") from exc
    if status is not None:
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise AppError("Factory plugin inventory directory is unsafe")
        if status.st_uid != os.getuid() or status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise AppError("Factory plugin inventory directory is unsafe")

        paths: list[PurePosixPath] = []
        entry_count = 0
        for candidate in metadata_root.iterdir():
            entry_count += 1
            if entry_count > _MAX_PLUGIN_METADATA_FILES:
                raise AppError("Factory plugin inventory has too many metadata files")
            if candidate.suffix != ".json":
                continue
            if candidate.parent != metadata_root or candidate.name in {".", ".."}:
                raise AppError("Factory plugin inventory file path is unsafe")
            status = os.stat(candidate, follow_symlinks=False)
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                raise AppError("Factory plugin inventory file is unsafe")
            if status.st_uid != os.getuid() or status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise AppError("Factory plugin inventory file is unsafe")
            paths.append(PLUGIN_INVENTORY_DIR / candidate.name)
        if paths:
            return tuple(sorted(paths, key=str))

    inventory = root / PLUGIN_INVENTORY
    try:
        status = os.stat(inventory, follow_symlinks=False)
    except FileNotFoundError:
        return (PLUGIN_INVENTORY,)
    except OSError as exc:
        raise AppError("Factory plugin inventory could not be inspected") from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise AppError("Factory plugin inventory must be a regular non-symlink file")
    return (PLUGIN_INVENTORY,)


def _inventory_from_snapshots(
    snapshots: Mapping[ArtifactIdentity, ArtifactSnapshot],
) -> bytes | AbsentDestination:
    metadata: list[bytes] = []
    metadata_bytes = 0
    for identity, snapshot in sorted(
        snapshots.items(),
        key=lambda item: item[0].relative_path.as_posix(),
    ):
        relative = identity.relative_path
        if (
            identity.grant_id != PLUGIN_GRANT
            or len(relative.parts) != 2
            or relative.parent != PLUGIN_INVENTORY_DIR
            or relative.suffix != ".json"
        ):
            continue
        if not isinstance(snapshot, bytes):
            raise AdapterPlanError(f"oh-my-droid plugin inventory file {relative} is absent")
        if len(metadata) >= _MAX_PLUGIN_METADATA_FILES:
            raise AdapterPlanError("oh-my-droid plugin inventory has too many metadata files")
        metadata_bytes += len(snapshot)
        if metadata_bytes > _MAX_PLUGIN_METADATA_BYTES:
            raise AdapterPlanError("oh-my-droid plugin inventory exceeds 16 MiB")
        metadata.append(snapshot)
    if metadata:
        return _canonical_inventory(metadata)
    legacy = snapshots.get(ArtifactIdentity(PLUGIN_GRANT, PLUGIN_INVENTORY))
    if isinstance(legacy, bytes):
        return legacy
    return AbsentDestination()


def _plugin_paths_from_snapshots(
    snapshots: Mapping[ArtifactIdentity, ArtifactSnapshot],
) -> dict[str, PurePosixPath]:
    paths: dict[str, PurePosixPath] = {}
    for identity, snapshot in snapshots.items():
        relative = identity.relative_path
        if (
            identity.grant_id != PLUGIN_GRANT
            or relative.suffix != ".md"
            or not _DROID_NAME_RE.fullmatch(relative.stem)
        ):
            continue
        if not isinstance(snapshot, bytes):
            raise AdapterPlanError(f"oh-my-droid source droid {relative} is absent")
        if relative.stem in paths and paths[relative.stem] != relative:
            raise AdapterPlanError(f"duplicate oh-my-droid plugin droid {relative.stem!r}")
        paths[relative.stem] = relative
    if not paths:
        raise AdapterPlanError("oh-my-droid plugin has no droid definitions")
    return paths


def _canonical_inventory(metadata: Sequence[bytes]) -> bytes:
    plugins: dict[str, list[dict[str, str]]] = {}
    for content in metadata:
        try:
            document = json.loads(content)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AdapterPlanError("oh-my-droid installed plugin inventory is malformed") from exc
        if not isinstance(document, Mapping):
            raise AdapterPlanError("oh-my-droid installed plugin inventory must be an object")
        plugin_id = document.get("pluginId")
        entry = document.get("entry")
        if (
            not isinstance(plugin_id, str)
            or not plugin_id
            or not isinstance(entry, Mapping)
            or not isinstance(entry.get("installPath"), str)
        ):
            raise AdapterPlanError("oh-my-droid installed plugin inventory is malformed")
        plugins.setdefault(plugin_id, []).append({"installPath": entry["installPath"]})
    return json.dumps({"plugins": plugins}, separators=(",", ":")).encode()


def _inventory_roots(inventory: bytes | AbsentDestination, root: Path) -> tuple[Path, ...]:
    if isinstance(inventory, AbsentDestination):
        cache_root = root / "cache" / "oh-my-droid" / "oh-my-droid"
        try:
            cache_status = os.stat(cache_root, follow_symlinks=False)
        except FileNotFoundError:
            return ()
        if stat.S_ISLNK(cache_status.st_mode) or not stat.S_ISDIR(cache_status.st_mode):
            return ()
        fallback_roots: list[Path] = []
        for version in sorted(cache_root.iterdir(), key=str):
            if version.is_symlink() or not version.is_dir():
                continue
            droids = version / "droids"
            try:
                status = os.stat(droids, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                continue
            if status.st_uid != os.getuid() or status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise AdapterPlanError("oh-my-droid plugin droids directory is unsafe")
            fallback_roots.append(droids)
        return tuple(fallback_roots)
    try:
        document = json.loads(inventory)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterPlanError("oh-my-droid installed plugin inventory is malformed") from exc
    if not isinstance(document, Mapping):
        raise AdapterPlanError("oh-my-droid installed plugin inventory must be an object")
    plugins = document.get("plugins")
    if not isinstance(plugins, Mapping):
        raise AdapterPlanError("oh-my-droid installed plugin inventory has no plugins map")
    entries = plugins.get("oh-my-droid@oh-my-droid")
    if not isinstance(entries, list):
        raise AdapterPlanError("oh-my-droid plugin is not installed")
    roots: list[Path] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("installPath"), str):
            continue
        install = Path(entry["installPath"]).expanduser().absolute()
        try:
            relative = install.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise AdapterPlanError("oh-my-droid install path is outside the plugin grant") from exc
        droids = (root / relative / "droids").absolute()
        try:
            status = os.stat(droids, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise AdapterPlanError("oh-my-droid plugin droids directory is unavailable") from exc
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise AdapterPlanError("oh-my-droid plugin droids directory is unsafe")
        if status.st_uid != os.getuid() or status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise AdapterPlanError("oh-my-droid plugin droids directory is unsafe")
        roots.append(droids)
    if not roots:
        raise AdapterPlanError("oh-my-droid plugin droids directory is unavailable")
    return tuple(dict.fromkeys(roots))


def _plugin_droid_paths(
    inventory: bytes | AbsentDestination, root: Path
) -> dict[str, PurePosixPath]:
    paths: dict[str, PurePosixPath] = {}
    for droids_root in _inventory_roots(inventory, root):
        for source in sorted(droids_root.iterdir(), key=str):
            if source.suffix != ".md" or not _DROID_NAME_RE.fullmatch(source.stem):
                continue
            status = os.stat(source, follow_symlinks=False)
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                raise AdapterPlanError(f"oh-my-droid source droid {source.stem!r} is unsafe")
            if status.st_size > _MAX_PLUGIN_ARTIFACT_BYTES:
                raise AdapterPlanError(f"oh-my-droid source droid {source.stem!r} exceeds 16 MiB")
            relative = PurePosixPath(source.absolute().relative_to(root).as_posix())
            if source.stem in paths and paths[source.stem] != relative:
                raise AdapterPlanError(f"duplicate oh-my-droid plugin droid {source.stem!r}")
            paths[source.stem] = relative
    if not paths:
        raise AdapterPlanError("oh-my-droid plugin has no droid definitions")
    return paths


def _inventory_snapshot(root: Path) -> bytes | AbsentDestination:
    try:
        artifacts = _inventory_artifacts(root)
        if artifacts == (PLUGIN_INVENTORY,):
            inventory = root / PLUGIN_INVENTORY
            try:
                os.lstat(inventory)
            except FileNotFoundError:
                return AbsentDestination()
            except OSError as exc:
                raise AppError("Factory plugin inventory could not be inspected") from exc
            return _read_plugin_bytes(inventory)
        metadata: list[bytes] = []
        metadata_bytes = 0
        for relative in artifacts:
            content = _read_plugin_bytes(root / relative)
            metadata_bytes += len(content)
            if metadata_bytes > _MAX_PLUGIN_METADATA_BYTES:
                raise AppError("Factory plugin inventory exceeds 16 MiB")
            metadata.append(content)
        return _canonical_inventory(metadata)
    except PrivateParentMissingError:
        return AbsentDestination()


def _rewrite_model_frontmatter(content: bytes, droid_name: str, model_id: str) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise AdapterPlanError(f"oh-my-droid source {droid_name!r} is not UTF-8") from exc
    if text.startswith("---"):
        lines = text.splitlines(keepends=True)
        close_index = next(
            (
                index
                for index in range(1, len(lines))
                if lines[index].rstrip("\r\n").strip() == "---"
            ),
            None,
        )
        if close_index is not None:
            frontmatter = lines[1:close_index]
            body = lines[close_index + 1 :]
            newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
            replaced = False
            updated: list[str] = []
            for line in frontmatter:
                line_end = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                line_value = line[: -len(line_end)] if line_end else line
                if _MODEL_LINE_RE.match(line_value) or line_value == "model:":
                    updated.append(f"model: {model_id}{line_end}")
                    replaced = True
                else:
                    updated.append(line)
            if not replaced:
                placed = False
                inserted: list[str] = []
                for line in updated:
                    inserted.append(line)
                    line_value = line.rstrip("\r\n")
                    if not placed and re.match(r"^name:\s", line_value):
                        inserted.append(f"model: {model_id}{newline}")
                        placed = True
                if not placed:
                    inserted.insert(0, f"model: {model_id}{newline}")
                updated = inserted
            opening = lines[0]
            closing = lines[close_index]
            return (
                opening.encode("utf-8")
                + b"".join(item.encode("utf-8") for item in updated)
                + closing.encode("utf-8")
                + b"".join(item.encode("utf-8") for item in body)
            )
    return f"---\nname: {droid_name}\nmodel: {model_id}\n---\n\n{text}".encode()


def _config_droids(config: Mapping[str, object]) -> tuple[Mapping[str, object], bool]:
    droids = config.get("droids")
    prune = config.get("prune", False)
    if not isinstance(droids, Mapping) or not isinstance(prune, bool):
        raise AdapterPlanError("oh-my-droid configuration is malformed")
    return droids, prune


def _owned_names(ownership: Mapping[str, object]) -> tuple[str, ...]:
    raw = ownership.get("droidNames", ())
    if not isinstance(raw, (list, tuple)) or not all(
        isinstance(name, str) and _DROID_NAME_RE.fullmatch(name) for name in raw
    ):
        raise AdapterPlanError("oh-my-droid ownership droidNames is invalid")
    names = tuple(raw)
    if len(names) != len(set(names)):
        raise AdapterPlanError("oh-my-droid ownership droidNames contains duplicates")
    return names


def _owned_hashes(ownership: Mapping[str, object]) -> Mapping[str, str]:
    raw = ownership.get("droidHashes", {})
    if not isinstance(raw, Mapping) or not all(
        isinstance(name, str)
        and _DROID_NAME_RE.fullmatch(name)
        and isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{64}", value)
        for name, value in raw.items()
    ):
        raise AdapterPlanError("oh-my-droid ownership droidHashes is invalid")
    return raw


def _owned_plugin_derived_names(ownership: Mapping[str, object]) -> tuple[str, ...]:
    raw = ownership.get("pluginDerivedNames", ())
    if not isinstance(raw, (list, tuple)) or not all(
        isinstance(name, str) and _DROID_NAME_RE.fullmatch(name) for name in raw
    ):
        raise AdapterPlanError("oh-my-droid ownership pluginDerivedNames is invalid")
    names = tuple(raw)
    if len(names) != len(set(names)):
        raise AdapterPlanError("oh-my-droid ownership pluginDerivedNames contains duplicates")
    return names


@dataclass(frozen=True)
class OhMyDroidAdapter:
    metadata: AdapterMetadata = _METADATA

    def describe(self) -> AdapterMetadata:
        return self.metadata

    def validate(self, config: Mapping[str, object], context: AdapterValidationContext) -> None:
        if context.logical_client != "factory" or context.component != _COMPONENT:
            raise AdapterPlanError("oh-my-droid adapter binding must be factory/oh-my-droid")
        if not config:
            return
        droids, prune = _config_droids(config)
        del prune
        for name, reference in droids.items():
            if not isinstance(name, str) or not _DROID_NAME_RE.fullmatch(name):
                raise AdapterPlanError(f"oh-my-droid droid name {name!r} is invalid")
            if isinstance(reference, InheritReference):
                continue
            if not isinstance(reference, ModelReference):
                raise AdapterPlanError(f"oh-my-droid droid {name!r} must use a model reference")
            try:
                context.resolve_model(reference)
            except (AppError, ValueError) as exc:
                raise AdapterPlanError(f"oh-my-droid droid {name!r}: {exc}") from exc
        paths = _plugin_droid_paths(_inventory_snapshot(_plugin_root()), _plugin_root())
        missing = sorted(set(droids) - set(paths))
        if missing:
            raise AdapterPlanError(f"oh-my-droid droids are not installed: {', '.join(missing)}")

    def preflight(self, context: AdapterContext) -> PreflightDeclaration:
        if context != AdapterContext("factory", _COMPONENT):
            raise AdapterPlanError("oh-my-droid adapter binding must be factory/oh-my-droid")
        root = _plugin_root()
        inventory = _inventory_snapshot(root)
        plugin_paths = _plugin_droid_paths(inventory, root)
        write_names = set(_safe_droid_names(_droids_root())) | set(plugin_paths)
        reads = [
            SnapshotRequest(ArtifactIdentity(PLUGIN_GRANT, path))
            for path in _inventory_artifacts(root)
        ]
        reads.extend(
            SnapshotRequest(ArtifactIdentity(PLUGIN_GRANT, path)) for path in plugin_paths.values()
        )
        writes = tuple(
            ProspectiveWrite(ArtifactIdentity(DROID_GRANT, PurePosixPath(f"{name}.md")))
            for name in sorted(write_names)
        )
        return PreflightDeclaration({}, tuple(reads), writes)

    def plan(
        self,
        context: AdapterPlanContext,
        proof: RuntimeProof | None,
        snapshots: Mapping[ArtifactIdentity, ArtifactSnapshot],
        ownership: Mapping[str, object],
    ) -> ArtifactPlan:
        del proof
        if context.logical_client != "factory" or context.component != _COMPONENT:
            raise AdapterPlanError("oh-my-droid adapter binding must be factory/oh-my-droid")
        config = context.selected_config
        droids, prune = _config_droids(config) if config else ({}, False)
        _inventory_from_snapshots(snapshots)
        plugin_paths = _plugin_paths_from_snapshots(snapshots)
        source_by_name: dict[str, bytes] = {}
        for name, relative in plugin_paths.items():
            source = snapshots.get(ArtifactIdentity(PLUGIN_GRANT, relative))
            if not isinstance(source, bytes):
                raise AdapterPlanError(f"oh-my-droid source droid {name!r} is absent")
            source_by_name[name] = source

        old_names = set(_owned_names(ownership))
        old_hashes = _owned_hashes(ownership)
        old_plugin_derived_names = set(_owned_plugin_derived_names(ownership))
        if set(old_hashes) != old_names:
            raise AdapterPlanError("oh-my-droid ownership names and hashes do not match")
        desired: dict[str, bytes] = {}
        for name, reference in droids.items():
            if name not in source_by_name:
                raise AdapterPlanError(f"oh-my-droid droid {name!r} is not in the plugin inventory")
            if isinstance(reference, InheritReference):
                desired[name] = _rewrite_model_frontmatter(
                    source_by_name[name], name, reference.inherit_marker
                )
                continue
            if not isinstance(reference, ModelReference):
                raise AdapterPlanError(f"oh-my-droid droid {name!r} must use a model reference")
            model = context.resolve_model(reference)
            desired[name] = _rewrite_model_frontmatter(source_by_name[name], name, model.factory_id)

        delete_names = set()
        if not config:
            delete_names = old_names & old_plugin_derived_names
        elif prune:
            delete_names = (old_names - set(desired)) & old_plugin_derived_names

        artifacts: list[PlannedArtifact] = []
        remaining = (old_names - delete_names) | set(desired)
        hashes: dict[str, str] = {
            name: old_hashes[name] for name in remaining if name in old_hashes
        }
        for name, planned in desired.items():
            identity = ArtifactIdentity(DROID_GRANT, PurePosixPath(f"{name}.md"))
            current = snapshots.get(identity, AbsentDestination())
            if (
                name in old_hashes
                and isinstance(current, bytes)
                and hashlib.sha256(current).hexdigest() != old_hashes[name]
            ):
                raise AdapterPlanError(f"oh-my-droid droid {name!r} has drifted")
            artifacts.append(PlannedArtifact(identity, planned, "features.oh-my-droid.droids", {}))
            hashes[name] = hashlib.sha256(planned).hexdigest()
        for name in sorted(delete_names):
            identity = ArtifactIdentity(DROID_GRANT, PurePosixPath(f"{name}.md"))
            current = snapshots.get(identity, AbsentDestination())
            if (
                name in old_hashes
                and isinstance(current, bytes)
                and hashlib.sha256(current).hexdigest() != old_hashes[name]
            ):
                raise AdapterPlanError(f"oh-my-droid droid {name!r} has drifted")
            if not isinstance(current, AbsentDestination):
                artifacts.append(
                    PlannedArtifact(
                        identity,
                        AbsentDestination(),
                        "features.oh-my-droid.droids",
                        {},
                    )
                )

        plugin_derived_names = old_plugin_derived_names & remaining
        for name in desired:
            if name in old_plugin_derived_names:
                plugin_derived_names.add(name)
                continue
            current = snapshots.get(
                ArtifactIdentity(DROID_GRANT, PurePosixPath(f"{name}.md")),
                AbsentDestination(),
            )
            if name not in old_names and isinstance(current, AbsentDestination):
                plugin_derived_names.add(name)

        return ArtifactPlan(
            tuple(artifacts),
            {
                "droidNames": sorted(remaining),
                "droidHashes": hashes,
                "pluginDerivedNames": sorted(plugin_derived_names),
            },
        )

    def recheck(self, proof: RuntimeProof | None) -> None:
        del proof

    def verify(
        self,
        context: AdapterContext,
        proof: RuntimeProof | None,
        written: Sequence[ArtifactSnapshot],
    ) -> None:
        del proof
        if context != AdapterContext("factory", _COMPONENT):
            raise AdapterPlanError("oh-my-droid adapter binding must be factory/oh-my-droid")
        for snapshot in written:
            if isinstance(snapshot, bytes):
                try:
                    snapshot.decode("utf-8")
                except UnicodeError as exc:
                    raise AdapterPlanError("oh-my-droid output is not UTF-8") from exc


adapter = OhMyDroidAdapter()
