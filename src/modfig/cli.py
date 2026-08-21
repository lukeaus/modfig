from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

import yaml

from .adapter_routes import (
    AdapterRoute,
    AdapterRoutes,
    PathGrant,
    builtin_adapter_routes,
    load_adapter_routes,
    local_adapter_routes_payload,
    resolve_adapter_routes_path,
    validate_adapter_routes,
)
from .adapters import discover_adapter_entry_points, load_enabled_adapter
from .app import load_valid_registry, run, validate_adapters, validate_logical_client
from .clients import chatgpt, vscode
from .components import Component, ExtensionComponent
from .errors import AppError
from .registry import RegistryValidationError
from .storage import (
    conditional_write_private_text,
    resolve_config_path,
    write_new_private_file,
)

INITIAL_REGISTRY = """\
# ModFig Registry Specification 0.1
specVersion: "0.1"

providers:
  example:
    name: Example Provider
    targets: [factory]
    baseUrl: https://api.example.com/v1
    apiKey: env.EXAMPLE_API_KEY
    enabled: true
    models:
      example-model:
        displayName: Example Model
        contextWindow: 8192
        maxOutputTokens: 1024
        enabled: true

clientConfig:
  factory:
    core:
      defaults:
        worker: {provider: example, model: example-model}
        thinker: {provider: example, model: example-model}
        orchestrator: {provider: example, model: example-model}
        simple: {provider: example, model: example-model}
        validator: {provider: example, model: example-model}
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="modfig")
    subcommands = parser.add_subparsers(dest="command", required=True)
    validate = subcommands.add_parser("validate")
    validate.add_argument("--config", metavar="FILE")
    validate.add_argument("--adapters", action="store_true")
    init = subcommands.add_parser("init")
    init.add_argument("--config", metavar="FILE")
    vscode = subcommands.add_parser("vscode")
    vscode_commands = vscode.add_subparsers(dest="vscode_command", required=True)
    proof = vscode_commands.add_parser("proof")
    proof_commands = proof.add_subparsers(dest="proof_command", required=True)
    capture = proof_commands.add_parser("capture")
    capture.add_argument("--output", metavar="FILE")
    capture.add_argument("--installation-root", metavar="DIR")
    chatgpt_command = subcommands.add_parser("chatgpt")
    chatgpt_commands = chatgpt_command.add_subparsers(dest="chatgpt_command", required=True)
    proof = chatgpt_commands.add_parser("proof")
    proof_commands = proof.add_subparsers(dest="proof_command", required=True)
    capture = proof_commands.add_parser("capture")
    capture.add_argument("--output", metavar="FILE")
    adapter = subcommands.add_parser("adapter")
    adapter_commands = adapter.add_subparsers(dest="adapter_command", required=True)
    enable = adapter_commands.add_parser("enable")
    enable.add_argument("entry_point")
    enable.add_argument("--client", required=True)
    component = enable.add_mutually_exclusive_group(required=True)
    component.add_argument("--extension")
    component.add_argument("--core", action="store_true")
    enable.add_argument("--distribution", required=True)
    enable.add_argument("--read-grant", action="append", default=[])
    enable.add_argument("--write-grant", action="append", default=[])
    disable = adapter_commands.add_parser("disable")
    disable.add_argument("entry_point")
    for name in ("diff", "apply"):
        command = subcommands.add_parser(name)
        command.add_argument("--config", metavar="FILE")
        command.add_argument("--target", default="all", type=_target_type)
        if name == "apply":
            command.add_argument("--yes", action="store_true")
    return parser


def _target_type(value: str) -> str:
    try:
        return validate_logical_client(value)
    except AppError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def load_local_adapter_routes(path: Path) -> AdapterRoutes:
    if not path.exists():
        return AdapterRoutes()
    return load_adapter_routes(path)


def _parse_cli_grant(value: str) -> PathGrant:
    try:
        grant_id, kind, root, *scope = value.split(":")
    except ValueError:
        raise AppError("grant must be ID:KIND:ROOT[:RELATIVE_SCOPE]") from None
    if kind not in {"file", "directory"} or (kind == "file" and scope) or len(scope) > 1:
        raise AppError("grant must be ID:file:ROOT or ID:directory:ROOT:RELATIVE_SCOPE")
    relative = PurePosixPath(scope[0]) if scope else None
    return PathGrant(grant_id, kind, Path(root), relative)  # type: ignore[arg-type]


def _write_local_routes(path: Path, routes: AdapterRoutes) -> None:
    serialized = yaml.safe_dump(local_adapter_routes_payload(routes), sort_keys=False)
    conditional_write_private_text(path, serialized, "adapter routes")


def _capture_vscode_proof(arguments: argparse.Namespace) -> None:
    record = vscode.capture_vscode_proof_record(
        installation_root=None
        if arguments.installation_root is None
        else Path(arguments.installation_root)
    )
    output = (
        Path(arguments.output).expanduser()
        if arguments.output
        else Path(
            os.environ.get("MODFIG_VSCODE_PROOF", "~/.modfig/vscode-runtime-proof.json")
        ).expanduser()
    )
    vscode.write_vscode_proof_record(record, output)


def _capture_chatgpt_proof(arguments: argparse.Namespace) -> None:
    environment = dict(os.environ)
    record = chatgpt.capture_chatgpt_proof_record(environ=environment)
    output = (
        Path(arguments.output).expanduser()
        if arguments.output
        else Path(
            os.environ.get("MODFIG_CHATGPT_PROOF", "~/.modfig/chatgpt-runtime-proof.json")
        ).expanduser()
    )
    chatgpt.write_chatgpt_proof_record(record, output)


def _enable_adapter(arguments: argparse.Namespace) -> None:
    component: Component = (
        ExtensionComponent(arguments.extension) if arguments.extension else "core"
    )
    route = AdapterRoute(
        arguments.client,
        component,
        arguments.entry_point,
        arguments.distribution,
        True,
        tuple(_parse_cli_grant(value) for value in arguments.read_grant),
        tuple(_parse_cli_grant(value) for value in arguments.write_grant),
    )
    available = discover_adapter_entry_points()
    path = resolve_adapter_routes_path(os.environ, Path.home())
    local = load_local_adapter_routes(path)
    updated = AdapterRoutes(tuple(local) + (route,))
    validated = validate_adapter_routes(builtin_adapter_routes(), updated)
    route = validated.by_adapter_id(route.adapter_id)
    load_enabled_adapter(route, entry_points=available)
    _write_local_routes(path, AdapterRoutes(tuple(item for item in validated if not item.builtin)))


def _disable_adapter(arguments: argparse.Namespace) -> None:
    # ponytail: fail closed until manifest v3 can identify manifest-owned routes safely.
    raise AppError(
        "adapter disable is unavailable: the current manifest cannot safely identify "
        "manifest-owned route state; route bytes are preserved unchanged"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "init":
            resolution = resolve_config_path(arguments.config)
            if resolution.source == "legacy":
                raise AppError(
                    "refusing to init at legacy ~/.modfig.yaml while it shadows the "
                    "default ~/.config/modfig/config.yaml; set --config or MODFIG_CONFIG "
                    "explicitly, or migrate the legacy file first"
                )
            write_new_private_file(resolution.path, INITIAL_REGISTRY)
            print(f"Created {resolution.path}")
            return 0
        if arguments.command == "validate":
            if arguments.adapters:
                validate_adapters(arguments.config)
            else:
                load_valid_registry(arguments.config)
            print("Registry is valid.")
            return 0
        if arguments.command == "adapter":
            if arguments.adapter_command == "enable":
                _enable_adapter(arguments)
            else:
                _disable_adapter(arguments)
            print(f"Adapter {arguments.entry_point} {arguments.adapter_command}d.")
            return 0
        if arguments.command == "vscode":
            if arguments.vscode_command == "proof" and arguments.proof_command == "capture":
                _capture_vscode_proof(arguments)
                print("Captured VS Code runtime proof.")
                return 0
            raise AppError("unsupported VS Code command")
        if arguments.command == "chatgpt":
            if arguments.chatgpt_command == "proof" and arguments.proof_command == "capture":
                _capture_chatgpt_proof(arguments)
                print("Captured ChatGPT runtime proof.")
                return 0
            raise AppError("unsupported ChatGPT command")
        if arguments.command in {"diff", "apply"}:
            run(
                arguments.command,
                arguments.config,
                target=arguments.target,
                yes=getattr(arguments, "yes", False),
            )
            return 0
        raise AppError(f"unsupported command: {arguments.command}")
    except (AppError, RegistryValidationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.exit_code if isinstance(exc, AppError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
