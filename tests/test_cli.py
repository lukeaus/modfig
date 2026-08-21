from __future__ import annotations

import os
from pathlib import Path

import pytest

from modfig.adapter_routes import AdapterRoute, PathGrant, load_adapter_routes
from modfig.adapters import AdapterMetadata, AdapterValidationContext
from modfig.cli import build_parser, main

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
def test_validate_returns_zero_for_valid_registry(tmp_path: Path) -> None:
    config = tmp_path / "modfig.yaml"
    config.write_text(VALID_REGISTRY, encoding="utf-8")
    config.chmod(0o600)

    assert main(["validate", "--config", str(config)]) == 0


@POSIX_SECURE_IO
def test_init_creates_valid_registry_and_refuses_overwrite(tmp_path: Path) -> None:
    config = tmp_path / "modfig.yaml"

    assert main(["init", "--config", str(config)]) == 0
    first_bytes = config.read_bytes()
    assert main(["validate", "--config", str(config)]) == 0
    assert main(["init", "--config", str(config)]) == 1
    assert config.read_bytes() == first_bytes


@POSIX_SECURE_IO
def test_validate_reports_missing_registry(tmp_path: Path) -> None:
    assert main(["validate", "--config", str(tmp_path / "absent.yaml")]) == 1


def test_vscode_proof_capture_parser_accepts_output_and_installation() -> None:
    parser = build_parser()
    arguments = parser.parse_args(["vscode", "proof", "capture", "--output", "proof.json"])
    assert arguments.output == "proof.json"
    arguments = parser.parse_args(["chatgpt", "proof", "capture", "--output", "proof.json"])
    assert arguments.output == "proof.json"

    parser = build_parser()

    assert parser.parse_args(["diff", "--target", "chatgpt"]).target == "chatgpt"
    assert parser.parse_args(["apply", "--target", "chatgpt", "--yes"]).target == "chatgpt"


def test_cli_accepts_locally_routed_logical_client_name() -> None:
    parser = build_parser()
    assert parser.parse_args(["diff", "--target", "cursor"]).target == "cursor"


def test_cli_rejects_invalid_logical_client_spelling() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["diff", "--target", "io.modfig.cursor"])


@POSIX_SECURE_IO
def test_default_init_refuses_legacy_only_setup(tmp_path: Path, monkeypatch, capsys) -> None:
    legacy = tmp_path / ".modfig.yaml"
    legacy.write_text(VALID_REGISTRY, encoding="utf-8")
    legacy.chmod(0o600)
    monkeypatch.delenv("MODFIG_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr("modfig.storage.Path.home", lambda: tmp_path)

    assert main(["init"]) == 1
    assert not (tmp_path / "xdg" / "modfig" / "config.yaml").exists()
    assert "migrat" in capsys.readouterr().err.lower()


class _Distribution:
    name = "example-cursor"

    @property
    def entry_points(self) -> list[object]:
        return [_EntryPoint()]


class _Adapter:
    def describe(self) -> AdapterMetadata:
        return AdapterMetadata("io.example.cursor", "cursor", "core")

    def validate(self, config: object, context: object) -> None:
        del config, context

    def preflight(self, context: object) -> object:
        del context
        raise AssertionError("not called")

    def plan(self, *args: object) -> object:
        del args
        raise AssertionError("not called")

    def recheck(self, proof: object) -> None:
        del proof

    def verify(self, *args: object) -> None:
        del args


class _EntryPoint:
    name = "io.example.cursor"
    group = "modfig.adapters.v1"
    value = "example_cursor:adapter"
    dist = _Distribution()

    def load(self) -> object:
        return _Adapter()


def _route(tmp_path: Path, *, enabled: bool = True) -> AdapterRoute:
    root = tmp_path / "cursor"
    root.mkdir(exist_ok=True)
    return AdapterRoute(
        "cursor",
        "core",
        "io.example.cursor",
        "example-cursor",
        enabled,
        (),
        (PathGrant("cursor-settings", "file", root / "settings.json", None),),
    )


@POSIX_SECURE_IO
def test_adapter_enable_writes_one_structured_local_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "adapters.yaml"
    (tmp_path / "cursor").mkdir()
    monkeypatch.setattr(
        "modfig.cli.discover_adapter_entry_points",
        lambda: {"io.example.cursor": _EntryPoint()},
    )
    monkeypatch.setattr("modfig.cli.resolve_adapter_routes_path", lambda *_: path)
    monkeypatch.setattr("modfig.cli.Path.home", lambda: tmp_path)

    assert (
        main(
            [
                "adapter",
                "enable",
                "io.example.cursor",
                "--client",
                "cursor",
                "--core",
                "--distribution",
                "example-cursor",
                "--write-grant",
                f"cursor-settings:file:{tmp_path / 'cursor' / 'settings.json'}",
            ]
        )
        == 0
    )
    route = load_adapter_routes(path, home=tmp_path).client_route("cursor")
    assert route.adapter_id == "io.example.cursor"
    assert route.enabled is True


@POSIX_SECURE_IO
def test_adapter_disable_fails_closed_and_preserves_route_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    path = tmp_path / "adapters.yaml"
    cursor_root = tmp_path / "cursor"
    cursor_root.mkdir()
    original = (
        f"""adapterConfigVersion: "1"
clients:
  cursor:
    adapter: io.example.cursor
    distribution: example-cursor
    enabled: true
    readGrants: []
    writeGrants:
      - id: cursor-settings
        kind: file
        root: {cursor_root / "settings.json"}
"""
    ).encode()
    path.write_bytes(original)
    path.chmod(0o600)
    monkeypatch.setattr("modfig.cli.resolve_adapter_routes_path", lambda *_: path)
    monkeypatch.setattr("modfig.cli.Path.home", lambda: tmp_path)

    assert main(["adapter", "disable", "io.example.cursor"]) == 1
    assert path.read_bytes() == original
    assert "disable" in capsys.readouterr().err.lower()


def test_adapter_enable_requires_explicit_core_or_extension_binding() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as raised:
        parser.parse_args(
            [
                "adapter",
                "enable",
                "io.example.cursor",
                "--client",
                "cursor",
                "--distribution",
                "example-cursor",
            ]
        )
    assert raised.value.code == 2


@POSIX_SECURE_IO
def test_adapter_enable_rejects_unsafe_grant_before_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry_point = _EntryPoint()
    monkeypatch.setattr(
        "modfig.cli.discover_adapter_entry_points",
        lambda: {entry_point.name: entry_point},
    )
    monkeypatch.setattr("modfig.cli.resolve_adapter_routes_path", lambda *_: tmp_path / "routes")
    monkeypatch.setattr(
        "modfig.cli.load_enabled_adapter",
        lambda *args, **kwargs: pytest.fail("adapter imported before grant validation"),
    )

    assert (
        main(
            [
                "adapter",
                "enable",
                entry_point.name,
                "--client",
                "cursor",
                "--core",
                "--distribution",
                entry_point.dist.name,
                "--write-grant",
                "cursor-settings:file:$HOME/settings.json",
            ]
        )
        == 1
    )


OPAQUE_ADAPTER_REGISTRY = (
    VALID_REGISTRY
    + """clientConfig:
  cursor:
    core:
      privateSchema:
        anyShape: true
"""
)


class _ValidatingAdapter(_Adapter):
    validated: list[object] = []

    def validate(self, config: object, context: AdapterValidationContext) -> None:
        self.validated.append(config)
        assert context.logical_client == "cursor"


class _ValidatingEntryPoint(_EntryPoint):
    def load(self) -> object:
        return _ValidatingAdapter()


@POSIX_SECURE_IO
def test_plain_validate_is_portable_but_validate_adapters_loads_only_selected_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "modfig.yaml"
    config.write_text(OPAQUE_ADAPTER_REGISTRY, encoding="utf-8")
    config.chmod(0o600)
    routes_path = tmp_path / "adapters.yaml"
    cursor_root = tmp_path / "cursor"
    cursor_root.mkdir()
    routes_path.write_text(
        f"""adapterConfigVersion: "1"
clients:
  cursor:
    adapter: io.example.cursor
    distribution: example-cursor
    enabled: true
    readGrants: []
    writeGrants:
      - id: cursor-settings
        kind: file
        root: {cursor_root / "settings.json"}
""",
        encoding="utf-8",
    )
    routes_path.chmod(0o600)
    monkeypatch.setattr("modfig.app.resolve_adapter_routes_path", lambda *_: routes_path)
    monkeypatch.setattr("modfig.app.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "modfig.app.discover_adapter_entry_points",
        lambda: {"io.example.cursor": _ValidatingEntryPoint()},
    )
    _ValidatingAdapter.validated.clear()

    assert main(["validate", "--config", str(config)]) == 0
    assert _ValidatingAdapter.validated == []
    assert main(["validate", "--adapters", "--config", str(config)]) == 0
    assert _ValidatingAdapter.validated == [{"privateSchema": {"anyShape": True}}]


BUILTIN_ONLY_REGISTRY = """specVersion: "0.1"
providers:
  example:
    name: Example
    targets: [chatgpt]
    baseUrl: https://api.example.com/v1
    apiKey: env.EXAMPLE_KEY
    provider: openai
    enabled: true
    extensions:
      chatgpt:
        default: true
    models:
      example-model:
        displayName: Example Model
        contextWindow: 8192
        maxOutputTokens: 1024
        enabled: true
clientConfig:
  chatgpt:
    core:
      profile: work
"""


@POSIX_SECURE_IO
def test_validate_adapters_rejects_unimplemented_builtin_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "modfig.yaml"
    config.write_text(BUILTIN_ONLY_REGISTRY, encoding="utf-8")
    config.chmod(0o600)
    routes_path = tmp_path / "adapters.yaml"
    assert not routes_path.exists()
    monkeypatch.setattr("modfig.app.resolve_adapter_routes_path", lambda *_: routes_path)
    monkeypatch.setattr("modfig.app.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "modfig.app.discover_adapter_entry_points",
        lambda: pytest.fail("no adapter should be discovered for built-in-only config"),
    )

    assert main(["validate", "--adapters", "--config", str(config)]) == 1
    assert not routes_path.exists()
