from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal

import yaml

from .components import Component, ExtensionComponent
from .errors import AppError
from .registry import LOGICAL_ID_RE, DuplicateKeySafeLoader, RegistryValidationError
from .storage import read_private_text


class AdapterRouteError(AppError):
    """Trusted local adapter routing is invalid."""


@dataclass(frozen=True)
class PathGrant:
    grant_id: str
    kind: Literal["file", "directory"]
    root: Path
    relative_scope: PurePosixPath | None

    @property
    def scope(self) -> Path:
        if self.kind == "file" or self.relative_scope == PurePosixPath("."):
            return self.root
        assert self.relative_scope is not None
        return self.root.joinpath(*self.relative_scope.parts)


@dataclass(frozen=True)
class AdapterRoute:
    logical_client: str
    component: Component
    adapter_id: str
    distribution: str
    enabled: bool
    read_grants: tuple[PathGrant, ...]
    write_grants: tuple[PathGrant, ...]
    builtin: bool = False


@dataclass(frozen=True)
class AdapterRoutes:
    _routes: tuple[AdapterRoute, ...] = ()

    def __iter__(self) -> Iterator[AdapterRoute]:
        return iter(self._routes)

    def route(self, logical_client: str, component: Component) -> AdapterRoute:
        for route in self._routes:
            if route.logical_client == logical_client and route.component == component:
                return route
        raise AdapterRouteError(f"no adapter route for {logical_client!r} {component!r}")

    def client_route(self, logical_client: str) -> AdapterRoute:
        return self.route(logical_client, "core")

    def by_adapter_id(self, adapter_id: str) -> AdapterRoute:
        for route in self._routes:
            if route.adapter_id == adapter_id:
                return route
        raise AdapterRouteError(f"no adapter route for {adapter_id!r}")


_ROUTE_FIELDS = frozenset({"adapter", "distribution", "enabled", "readGrants", "writeGrants"})
_GRANT_FIELDS = frozenset({"id", "kind", "root", "relativeScope"})


def resolve_adapter_routes_path(environ: Mapping[str, str], home: Path) -> Path:
    configured = environ.get("XDG_CONFIG_HOME")
    root = Path(configured) if configured else None
    if root is None or not root.is_absolute():
        root = home / ".config"
    return root / "modfig" / "adapters.yaml"


CODEX_MODEL_CATALOG_FILENAME = "modfig-model-catalog.json"


def resolve_codex_home(environ: Mapping[str, str], home: Path) -> Path:
    configured = environ.get("CODEX_HOME")
    if configured is not None:
        root = Path(configured)
        if not configured or not root.is_absolute():
            raise ValueError("CODEX_HOME must be a non-empty absolute path")
    else:
        root = home / ".codex"
    return root.absolute()


def resolve_codex_config_path(environ: Mapping[str, str], home: Path) -> Path:
    return resolve_codex_home(environ, home) / "config.toml"


def resolve_codex_profile_config_path(
    environ: Mapping[str, str], home: Path, profile: str = "modfig"
) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", profile):
        raise ValueError("Codex profile name must be a safe non-empty identifier")
    return resolve_codex_home(environ, home) / f"{profile}.config.toml"


def resolve_codex_model_catalog_path(environ: Mapping[str, str], home: Path) -> Path:
    return resolve_codex_home(environ, home) / CODEX_MODEL_CATALOG_FILENAME


def builtin_adapter_routes(
    home: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    vscode_user_data_root: Path | None = None,
    include_chatgpt: bool = True,
) -> AdapterRoutes:
    root = Path.home() if home is None else home
    environment = {} if environ is None else environ
    vscode_user = (
        root / ".config" / "Code" / "User"
        if vscode_user_data_root is None
        else vscode_user_data_root
    )
    vscode_config = _builtin(
        "vscode",
        "modfig.vscode",
        vscode_user / "chatLanguageModels.json",
    )
    vscode_state = PathGrant(
        "vscode-state",
        "directory",
        vscode_user / "globalStorage",
        PurePosixPath("."),
    )
    vscode_route = AdapterRoute(
        "vscode",
        "core",
        "modfig.vscode",
        "modfig",
        True,
        (*vscode_config.read_grants, vscode_state),
        (*vscode_config.write_grants, vscode_state),
        True,
    )
    oh_my_droid_route = AdapterRoute(
        "factory",
        ExtensionComponent("oh-my-droid"),
        "modfig.oh_my_droid",
        "modfig",
        True,
        (
            PathGrant(
                "factory-plugins",
                "directory",
                root / ".factory" / "plugins",
                PurePosixPath("."),
            ),
        ),
        (
            PathGrant(
                "factory-droids",
                "directory",
                root / ".factory" / "droids",
                PurePosixPath("."),
            ),
        ),
        True,
    )
    routes = [
        _builtin("factory", "modfig.factory", root / ".factory" / "settings.json"),
        oh_my_droid_route,
        vscode_route,
    ]
    if include_chatgpt:
        try:
            configured_codex_home = "CODEX_HOME" in environment
            chatgpt_home = _trusted_codex_config_path(
                resolve_codex_config_path(environment, root),
                root,
                check_trust=configured_codex_home,
            ).parent
        except ValueError as exc:
            raise AdapterRouteError(str(exc)) from exc
        chatgpt_grant = PathGrant(
            "chatgpt-home",
            "directory",
            chatgpt_home,
            PurePosixPath("."),
        )
        routes.append(
            AdapterRoute(
                "chatgpt",
                "core",
                "modfig.chatgpt",
                "modfig",
                True,
                (chatgpt_grant,),
                (chatgpt_grant,),
                True,
            )
        )
    return AdapterRoutes(tuple(routes))


def _trusted_codex_config_path(config_path: Path, home: Path, *, check_trust: bool) -> Path:
    del home
    codex_home = config_path.parent
    if not check_trust or not codex_home.exists():
        return config_path
    current = Path(codex_home.anchor)
    for part in codex_home.relative_to(current).parts:
        current /= part
        if current.is_symlink():
            raise AdapterRouteError("CODEX_HOME ancestry must not contain symlinks")
        if current.exists():
            status = current.stat()
            if not stat.S_ISDIR(status.st_mode):
                raise AdapterRouteError("CODEX_HOME must be a directory")
            if status.st_uid not in (0, os.getuid()) or (
                status.st_uid == os.getuid() and status.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise AdapterRouteError("CODEX_HOME must be a private directory owned by the user")
    if config_path.is_symlink() or (config_path.exists() and not config_path.is_file()):
        raise AdapterRouteError("Codex config must be a regular non-symlink file")
    return config_path


def _builtin(logical_client: str, adapter_id: str, path: Path) -> AdapterRoute:
    grant = PathGrant(f"{logical_client}-config", "file", path, None)
    return AdapterRoute(
        logical_client,
        "core",
        adapter_id,
        "modfig",
        True,
        (grant,),
        (grant,),
        True,
    )


def load_adapter_routes(path: Path, *, home: Path | None = None) -> AdapterRoutes:
    trusted_home = (Path.home() if home is None else home).absolute()
    try:
        raw = yaml.load(read_private_text(path, "adapter routes"), Loader=DuplicateKeySafeLoader)
    except RegistryValidationError as exc:
        raise AdapterRouteError(str(exc)) from exc
    except yaml.YAMLError as exc:
        raise AdapterRouteError(f"invalid adapter routes YAML: {exc}") from exc
    root = _mapping(raw, "adapter routes")
    _exact_fields(root, {"adapterConfigVersion"}, {"clients", "extensions"}, "adapter routes")
    if root.get("adapterConfigVersion") != "1":
        raise AdapterRouteError('adapterConfigVersion must be "1"')
    routes: list[AdapterRoute] = []
    clients = _mapping(root.get("clients", {}), "clients")
    for logical_client, value in clients.items():
        _logical_id(logical_client, "client")
        routes.append(_parse_route(logical_client, "core", value, trusted_home))
    extensions = _mapping(root.get("extensions", {}), "extensions")
    for logical_client, extension_values in extensions.items():
        _logical_id(logical_client, "client")
        extension_map = _mapping(extension_values, f"extensions.{logical_client}")
        for extension, value in extension_map.items():
            try:
                component = ExtensionComponent(extension)
            except ValueError as exc:
                raise AdapterRouteError(str(exc)) from exc
            routes.append(_parse_route(logical_client, component, value, trusted_home))
    _unique_routes(routes)
    return AdapterRoutes(tuple(routes))


def validate_adapter_routes(
    builtins: AdapterRoutes, local: AdapterRoutes, *, home: Path | None = None
) -> AdapterRoutes:
    trusted_home = (Path.home() if home is None else home).absolute()
    canonical = AdapterRoutes(
        tuple(
            route
            if route.builtin
            else AdapterRoute(
                route.logical_client,
                route.component,
                route.adapter_id,
                route.distribution,
                route.enabled,
                tuple(_canonical_grant(grant, trusted_home) for grant in route.read_grants),
                tuple(_canonical_grant(grant, trusted_home) for grant in route.write_grants),
            )
            for route in local
        )
    )
    return merge_adapter_routes(builtins, canonical)


def _canonical_grant(grant: PathGrant, home: Path) -> PathGrant:
    root_value = str(grant.root)
    if "$" in root_value or "%" in root_value:
        raise AdapterRouteError("grant root must not use environment expansion")
    root = grant.root
    if root_value == "~" or root_value.startswith("~/"):
        root = home / root_value.removeprefix("~/") if root_value != "~" else home
    if not root.is_absolute():
        raise AdapterRouteError("grant root must be absolute or home-relative")
    root = _canonical_root(root, home, grant.kind == "file", "grant")
    if grant.kind == "file":
        if grant.relative_scope is not None or (root.exists() and not root.is_file()):
            raise AdapterRouteError("file grant must name a file and omit relativeScope")
    else:
        scope = grant.relative_scope
        if not root.is_dir() or scope is None or scope.is_absolute() or ".." in scope.parts:
            raise AdapterRouteError("directory grant must have a safe relativeScope")
    return PathGrant(grant.grant_id, grant.kind, root, grant.relative_scope)


def merge_adapter_routes(builtins: AdapterRoutes, local: AdapterRoutes) -> AdapterRoutes:
    routes = tuple(builtins) + tuple(local)
    _unique_routes(routes)
    _nonoverlapping_writes(routes)
    return AdapterRoutes(routes)


def local_adapter_routes_payload(routes: AdapterRoutes) -> dict[str, object]:
    clients: dict[str, object] = {}
    extensions: dict[str, dict[str, object]] = {}
    for route in routes:
        if route.builtin:
            continue
        value = _route_payload(route)
        if route.component == "core":
            clients[route.logical_client] = value
        else:
            extensions.setdefault(route.logical_client, {})[route.component.name] = value
    payload: dict[str, object] = {"adapterConfigVersion": "1"}
    if clients:
        payload["clients"] = clients
    if extensions:
        payload["extensions"] = extensions
    return payload


def _route_payload(route: AdapterRoute) -> dict[str, object]:
    return {
        "adapter": route.adapter_id,
        "distribution": route.distribution,
        "enabled": route.enabled,
        "readGrants": [_grant_payload(grant) for grant in route.read_grants],
        "writeGrants": [_grant_payload(grant) for grant in route.write_grants],
    }


def _grant_payload(grant: PathGrant) -> dict[str, object]:
    value: dict[str, object] = {
        "id": grant.grant_id,
        "kind": grant.kind,
        "root": str(grant.root),
    }
    if grant.relative_scope is not None:
        value["relativeScope"] = str(grant.relative_scope)
    return value


def _parse_route(
    logical_client: str, component: Component, raw: object, home: Path
) -> AdapterRoute:
    location = f"{logical_client}.{component if component == 'core' else component.name}"
    value = _mapping(raw, location)
    _exact_fields(value, _ROUTE_FIELDS, set(), location)
    adapter_id = _string(value, "adapter", location)
    distribution = _string(value, "distribution", location)
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        raise AdapterRouteError(f"{location}.enabled must be boolean")
    read_grants = _parse_grants(value.get("readGrants"), f"{location}.readGrants", home)
    write_grants = _parse_grants(value.get("writeGrants"), f"{location}.writeGrants", home)
    grant_ids = [grant.grant_id for grant in read_grants + write_grants]
    if len(grant_ids) != len(set(grant_ids)):
        raise AdapterRouteError(f"{location} has duplicate grant IDs")
    return AdapterRoute(
        logical_client,
        component,
        adapter_id,
        distribution,
        enabled,
        read_grants,
        write_grants,
    )


def _parse_grants(raw: object, location: str, home: Path) -> tuple[PathGrant, ...]:
    if not isinstance(raw, list):
        raise AdapterRouteError(f"{location} must be a list")
    result = tuple(
        _parse_grant(value, f"{location}[{index}]", home) for index, value in enumerate(raw)
    )
    ids = [grant.grant_id for grant in result]
    if len(ids) != len(set(ids)):
        raise AdapterRouteError(f"{location} has duplicate grant IDs")
    return result


def _parse_grant(raw: object, location: str, home: Path) -> PathGrant:
    value = _mapping(raw, location)
    kind = value.get("kind")
    required = (
        {"id", "kind", "root", "relativeScope"} if kind == "directory" else {"id", "kind", "root"}
    )
    _exact_fields(value, required, set(), location)
    grant_id = _string(value, "id", location)
    root_value = _string(value, "root", location)
    if "$" in root_value or "%" in root_value:
        raise AdapterRouteError(f"{location}.root must not use environment expansion")
    root = Path(root_value)
    if root_value == "~" or root_value.startswith("~/"):
        root = home / root_value.removeprefix("~/") if root_value != "~" else home
    if not root.is_absolute():
        raise AdapterRouteError(f"{location}.root must be absolute or home-relative")
    root = _canonical_root(root, home, kind == "file", location)
    if kind == "file":
        if root.exists() and not root.is_file():
            raise AdapterRouteError(f"{location}.root must be a regular file")
        return PathGrant(grant_id, "file", root, None)
    if kind != "directory":
        raise AdapterRouteError(f"{location}.kind must be 'file' or 'directory'")
    if not root.is_dir():
        raise AdapterRouteError(f"{location}.root must be an existing directory")
    scope_value = _string(value, "relativeScope", location)
    scope = PurePosixPath(scope_value)
    if scope.is_absolute() or ".." in scope.parts or str(scope) != scope_value:
        raise AdapterRouteError(f"{location}.relativeScope must be a normalized relative path")
    return PathGrant(grant_id, "directory", root, scope)


def _canonical_root(path: Path, home: Path, absent_leaf_ok: bool, location: str) -> Path:
    current = home.resolve()
    try:
        relative = path.relative_to(home)
    except ValueError:
        raise AdapterRouteError(f"{location}.root must be within the effective-user home") from None
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise AdapterRouteError(f"{location}.root ancestry must not contain symlinks")
        if not current.exists():
            break
    if path.exists():
        canonical = path.resolve()
    elif absent_leaf_ok:
        if not path.parent.exists():
            raise AdapterRouteError(f"{location}.root parent must be an existing trusted directory")
        canonical = path.parent.resolve() / path.name
    else:
        canonical = path.absolute()
    try:
        canonical.relative_to(home.resolve())
    except ValueError:
        raise AdapterRouteError(f"{location}.root must be within the effective-user home") from None
    return canonical


def _unique_routes(routes: tuple[AdapterRoute, ...] | list[AdapterRoute]) -> None:
    adapter_ids: set[str] = set()
    bindings: set[tuple[str, Component]] = set()
    for route in routes:
        binding = (route.logical_client, route.component)
        if route.adapter_id in adapter_ids:
            raise AdapterRouteError(f"adapter ID collision: {route.adapter_id}")
        if binding in bindings:
            raise AdapterRouteError(f"adapter route binding collision: {binding!r}")
        adapter_ids.add(route.adapter_id)
        bindings.add(binding)


def _nonoverlapping_writes(routes: tuple[AdapterRoute, ...]) -> None:
    scopes: list[tuple[Path, AdapterRoute]] = []
    for route in routes:
        if not route.enabled:
            continue
        for grant in route.write_grants:
            scope = grant.scope
            for other_scope, other_route in scopes:
                if (
                    scope == other_scope
                    or scope.is_relative_to(other_scope)
                    or other_scope.is_relative_to(scope)
                ):
                    raise AdapterRouteError(
                        "write grant overlap between "
                        f"{route.adapter_id} and {other_route.adapter_id}"
                    )
            scopes.append((scope, route))


def _mapping(value: object, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise AdapterRouteError(f"{location} must be a mapping with string keys")
    return MappingProxyType(dict(value))


def _exact_fields(
    value: Mapping[str, object],
    required: set[str] | frozenset[str],
    optional: set[str],
    location: str,
) -> None:
    unknown = set(value) - required - optional
    missing = required - set(value)
    if unknown:
        raise AdapterRouteError(f"{location} has unknown fields: {', '.join(sorted(unknown))}")
    if missing:
        raise AdapterRouteError(f"{location} is missing fields: {', '.join(sorted(missing))}")


def _string(value: Mapping[str, object], key: str, location: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise AdapterRouteError(f"{location}.{key} must be a non-empty string")
    return item


def _logical_id(value: str, label: str) -> None:
    if not LOGICAL_ID_RE.fullmatch(value):
        raise AdapterRouteError(f"invalid logical {label} name: {value!r}")
