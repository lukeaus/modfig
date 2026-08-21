from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

from modfig.adapter_routes import (
    AdapterRouteError,
    builtin_adapter_routes,
    load_adapter_routes,
    merge_adapter_routes,
    resolve_adapter_routes_path,
    resolve_codex_profile_config_path,
)
from modfig.components import ExtensionComponent

POSIX_SECURE_IO = pytest.mark.skipif(os.name == "nt", reason="requires POSIX ownership")


def _write_routes(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_builtin_routes_are_immutable_and_bind_core_clients() -> None:
    routes = builtin_adapter_routes()

    assert routes.client_route("factory").adapter_id == "modfig.factory"
    assert routes.client_route("vscode").adapter_id == "modfig.vscode"
    assert routes.client_route("chatgpt").adapter_id == "modfig.chatgpt"
    assert all(route.enabled and route.builtin for route in routes)


def test_adapter_routes_path_uses_only_absolute_nonempty_xdg(tmp_path: Path) -> None:
    assert resolve_adapter_routes_path({"XDG_CONFIG_HOME": str(tmp_path)}, tmp_path) == (
        tmp_path / "modfig" / "adapters.yaml"
    )
    fallback = tmp_path / ".config" / "modfig" / "adapters.yaml"
    assert resolve_adapter_routes_path({"XDG_CONFIG_HOME": ""}, tmp_path) == fallback
    assert resolve_adapter_routes_path({"XDG_CONFIG_HOME": "relative"}, tmp_path) == fallback


def test_codex_profile_config_path_uses_dedicated_modfig_profile(tmp_path: Path) -> None:
    assert resolve_codex_profile_config_path({}, tmp_path) == (
        tmp_path / ".codex" / "modfig.config.toml"
    )
    with pytest.raises(ValueError, match="profile name"):
        resolve_codex_profile_config_path({}, tmp_path, "../unsafe")


def test_builtin_vscode_routes_do_not_resolve_unselected_chatgpt_home(tmp_path: Path) -> None:
    routes = builtin_adapter_routes(
        home=tmp_path,
        environ={"CODEX_HOME": "relative"},
        include_chatgpt=False,
    )

    assert routes.client_route("vscode").adapter_id == "modfig.vscode"
    with pytest.raises(AdapterRouteError, match="CODEX_HOME"):
        builtin_adapter_routes(
            home=tmp_path,
            environ={"CODEX_HOME": "relative"},
            include_chatgpt=True,
        )


@POSIX_SECURE_IO
def test_loads_structured_local_routes_without_entry_point_field(tmp_path: Path) -> None:
    root = tmp_path / "cursor"
    root.mkdir()
    path = _write_routes(
        tmp_path / "adapters.yaml",
        f"""adapterConfigVersion: "1"
clients:
  cursor:
    adapter: io.example.cursor
    distribution: example-cursor
    enabled: true
    readGrants:
      - id: cursor-read
        kind: directory
        root: {root}
        relativeScope: .
    writeGrants:
      - id: cursor-write
        kind: file
        root: {root / "settings.json"}
extensions:
  factory:
    helper:
      adapter: io.example.helper
      distribution: example-helper
      enabled: false
      readGrants: []
      writeGrants: []
""",
    )

    routes = load_adapter_routes(path, home=tmp_path)
    cursor = routes.client_route("cursor")
    helper = routes.route("factory", ExtensionComponent("helper"))

    assert cursor.adapter_id == "io.example.cursor"
    assert cursor.read_grants[0].relative_scope == PurePosixPath(".")
    assert cursor.write_grants[0].relative_scope is None
    assert helper.enabled is False


@POSIX_SECURE_IO
@pytest.mark.parametrize(
    "fragment, message",
    [
        ("    entryPoint: example:adapter\n", "unknown.*entryPoint"),
        ("    adapter: modfig.factory\n", "adapter.*collision"),
    ],
)
def test_rejects_unknown_fields_and_builtin_adapter_shadowing(
    tmp_path: Path, fragment: str, message: str
) -> None:
    root = tmp_path / "cursor"
    root.mkdir()
    adapter_line = "    adapter: io.example.cursor\n"
    if "adapter:" in fragment:
        adapter_line = ""
    path = _write_routes(
        tmp_path / "adapters.yaml",
        f"""adapterConfigVersion: "1"
clients:
  cursor:
{adapter_line}{fragment}    distribution: example-cursor
    enabled: true
    readGrants: []
    writeGrants:
      - id: cursor-write
        kind: file
        root: {root / "settings.json"}
""",
    )

    with pytest.raises(AdapterRouteError, match=message):
        merge_adapter_routes(
            builtin_adapter_routes(home=tmp_path), load_adapter_routes(path, home=tmp_path)
        )


@POSIX_SECURE_IO
def test_rejects_overlapping_enabled_write_grants(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    path = _write_routes(
        tmp_path / "adapters.yaml",
        f"""adapterConfigVersion: "1"
clients:
  cursor:
    adapter: io.example.cursor
    distribution: example-cursor
    enabled: true
    readGrants: []
    writeGrants:
      - id: cursor-write
        kind: directory
        root: {root}
        relativeScope: .
extensions:
  cursor:
    helper:
      adapter: io.example.helper
      distribution: example-helper
      enabled: true
      readGrants: []
      writeGrants:
        - id: helper-write
          kind: file
          root: {root / "settings.json"}
""",
    )

    with pytest.raises(AdapterRouteError, match="overlap"):
        merge_adapter_routes(
            builtin_adapter_routes(home=tmp_path), load_adapter_routes(path, home=tmp_path)
        )


@POSIX_SECURE_IO
@pytest.mark.parametrize(
    "root, scope, message",
    [
        ("$HOME/cursor", ".", "environment"),
        ("{home}/cursor", "../other", "relativeScope"),
        ("{outside}", ".", "home"),
    ],
)
def test_rejects_unsafe_grants(tmp_path: Path, root: str, scope: str, message: str) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "cursor").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    path = _write_routes(
        home / "adapters.yaml",
        f"""adapterConfigVersion: "1"
clients:
  cursor:
    adapter: io.example.cursor
    distribution: example-cursor
    enabled: true
    readGrants: []
    writeGrants:
      - id: cursor-write
        kind: directory
        root: {root.format(home=home, outside=outside)}
        relativeScope: {scope}
""",
    )

    with pytest.raises(AdapterRouteError, match=message):
        load_adapter_routes(path, home=home)


@POSIX_SECURE_IO
def test_grant_ids_are_unique_across_route_read_and_write_sets(tmp_path: Path) -> None:
    root = tmp_path / "cursor"
    root.mkdir()
    path = _write_routes(
        tmp_path / "adapters.yaml",
        f"""adapterConfigVersion: "1"
clients:
  cursor:
    adapter: io.example.cursor
    distribution: example-cursor
    enabled: true
    readGrants:
      - id: cursor-config
        kind: directory
        root: {root}
        relativeScope: .
    writeGrants:
      - id: cursor-config
        kind: file
        root: {root / "settings.json"}
""",
    )

    with pytest.raises(AdapterRouteError, match="duplicate grant IDs"):
        load_adapter_routes(path, home=tmp_path)


@POSIX_SECURE_IO
def test_rejects_symlink_in_grant_ancestry(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    path = _write_routes(
        tmp_path / "adapters.yaml",
        f"""adapterConfigVersion: "1"
clients:
  cursor:
    adapter: io.example.cursor
    distribution: example-cursor
    enabled: true
    readGrants: []
    writeGrants:
      - id: cursor-write
        kind: file
        root: {linked / "settings.json"}
""",
    )

    with pytest.raises(AdapterRouteError, match="symlink"):
        load_adapter_routes(path, home=tmp_path)


def test_builtin_routes_use_explicit_vscode_user_data_root(tmp_path: Path) -> None:
    vscode_user = tmp_path / "Library" / "Application Support" / "Code" / "User"

    route = builtin_adapter_routes(home=tmp_path, vscode_user_data_root=vscode_user).client_route(
        "vscode"
    )

    assert route.read_grants[0].root == vscode_user / "chatLanguageModels.json"
    assert route.read_grants[1].root == vscode_user / "globalStorage"


def test_builtin_grants_are_exact_home_files() -> None:
    routes = builtin_adapter_routes(home=Path("/home/user"))

    factory = routes.client_route("factory")
    assert factory.read_grants[0].kind == "file"
    assert factory.read_grants[0].root == Path("/home/user/.factory/settings.json")

    vscode = routes.client_route("vscode")
    assert vscode.read_grants[0].kind == "file"
    assert vscode.read_grants[0].root == Path(
        "/home/user/.config/Code/User/chatLanguageModels.json"
    )
    assert [grant.grant_id for grant in vscode.read_grants] == [
        "vscode-config",
        "vscode-state",
    ]
    assert vscode.read_grants[1].kind == "directory"
    assert vscode.read_grants[1].root == Path("/home/user/.config/Code/User/globalStorage")

    chatgpt = routes.client_route("chatgpt")
    assert [grant.grant_id for grant in chatgpt.read_grants] == ["chatgpt-home"]
    assert chatgpt.read_grants[0].kind == "directory"
    assert chatgpt.read_grants[0].root == Path("/home/user/.codex")
    assert chatgpt.read_grants[0].relative_scope == PurePosixPath(".")
    assert [grant.grant_id for grant in chatgpt.write_grants] == ["chatgpt-home"]


@POSIX_SECURE_IO
def test_factory_extension_grant_coexists_with_builtin_core(tmp_path: Path) -> None:
    home = tmp_path / "home"
    droids_dir = home / ".factory" / "custom-droids"
    droids_dir.mkdir(parents=True)
    path = _write_routes(
        home / "adapters.yaml",
        f"""adapterConfigVersion: "1"
extensions:
  factory:
    droids:
      adapter: io.example.droids
      distribution: example-droids
      enabled: true
      readGrants: []
      writeGrants:
        - id: droids-write
          kind: directory
          root: {droids_dir}
          relativeScope: .
""",
    )

    routes = merge_adapter_routes(
        builtin_adapter_routes(home=home), load_adapter_routes(path, home=home)
    )
    builtin = routes.client_route("factory")
    assert builtin.write_grants[0].kind == "file"
    assert builtin.write_grants[0].root == home / ".factory" / "settings.json"
    extension = routes.route("factory", ExtensionComponent("droids"))
    assert extension.enabled is True
    assert extension.write_grants[0].scope == droids_dir
