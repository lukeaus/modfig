"""Transport preflight probe tests (VAL-PROBE-001..004, VAL-CROSS-002).

These tests exercise the fail-closed Responses and Messages probes against a
local HTTP stub so no real provider is contacted and no real credentials are
used.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from modfig import app
from modfig.cli import main
from modfig.clients.factory import probe_factory_models
from modfig.errors import AppError
from modfig.registry import load_registry_text

POSIX_SECURE_IO = pytest.mark.skipif(os.name == "nt", reason="requires native POSIX secure I/O")

KEY_SENTINEL = "sk-probe-sentinel-DO-NOT-LEAK"
BODY_SENTINEL = b"INTERNAL-RESPONSE-BODY-DO-NOT-LEAK"


def _openai_factory_registry(base_url: str, *, model: str = "primary") -> str:
    return (
        f'specVersion: "0.1"\n'
        f"providers:\n"
        f"  router:\n"
        f"    name: Router\n"
        f"    targets: [factory]\n"
        f"    baseUrl: {base_url}\n"
        f"    apiKey: env.ROUTER_KEY\n"
        f"    provider: openai\n"
        f"    enabled: true\n"
        f"    models:\n"
        f"      {model}:\n"
        f"        displayName: Primary\n"
        f"        contextWindow: 8192\n"
        f"        maxOutputTokens: 1024\n"
        f"        enabled: true\n"
    )


def _mixed_factory_registry(openai_base: str, generic_base: str) -> str:
    return (
        f'specVersion: "0.1"\n'
        f"providers:\n"
        f"  openai-router:\n"
        f"    name: OpenAI Router\n"
        f"    targets: [factory]\n"
        f"    baseUrl: {openai_base}\n"
        f"    apiKey: env.OPENAI_KEY\n"
        f"    provider: openai\n"
        f"    enabled: true\n"
        f"    models:\n"
        f"      openai-model:\n"
        f"        displayName: OpenAI Model\n"
        f"        contextWindow: 8192\n"
        f"        maxOutputTokens: 1024\n"
        f"        enabled: true\n"
        f"  generic-router:\n"
        f"    name: Generic Router\n"
        f"    targets: [factory]\n"
        f"    baseUrl: {generic_base}\n"
        f"    apiKey: env.GENERIC_KEY\n"
        f"    provider: generic-chat-completion-api\n"
        f"    enabled: true\n"
        f"    models:\n"
        f"      generic-model:\n"
        f"        displayName: Generic Model\n"
        f"        contextWindow: 8192\n"
        f"        maxOutputTokens: 1024\n"
        f"        enabled: true\n"
    )


class _ProbeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, behaviors: Sequence[dict[str, Any]]):
        super().__init__(("127.0.0.1", 0), _ProbeHandler)
        self._behaviors = list(behaviors)
        self.requests: list[dict[str, Any]] = []

    def next_behavior(self) -> dict[str, Any]:
        if not self._behaviors:
            return {"status": 200, "body": b'{"output": [{"type": "message"}]}', "delay": 0.0}
        return self._behaviors.pop(0)


class _ProbeHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server contract
        server: _ProbeServer = self.server  # type: ignore[assignment]
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        behavior = server.next_behavior()
        if behavior.get("delay"):
            time.sleep(behavior["delay"])
        server.requests.append(
            {
                "path": self.path,
                "body": body,
                "auth": self.headers.get("Authorization", ""),
                "x_api_key": self.headers.get("x-api-key", ""),
                "anthropic_version": self.headers.get("anthropic-version", ""),
            }
        )
        payload = behavior["body"]
        self.send_response(behavior["status"])
        if "location" in behavior:
            self.send_header("Location", behavior["location"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:  # silence test stderr
        return


@contextmanager
def _stub_server(behaviors: Sequence[dict[str, Any]] = ()) -> Iterator[_ProbeServer]:
    server = _ProbeServer(behaviors)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _write_registry(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _closed_loopback_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# --- VAL-PROBE-001: plain validate stays offline and deterministic ---


@POSIX_SECURE_IO
def test_plain_validate_does_not_probe_resolve_secrets_or_open_sockets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "modfig.yaml"
    _write_registry(config, _openai_factory_registry("https://router.example/v1"))

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("plain validate must not probe, resolve secrets, or open sockets")

    monkeypatch.setattr("modfig.clients.factory.probe_factory_models", fail)
    monkeypatch.setattr("modfig.clients.factory.resolve_secret", fail)
    monkeypatch.setattr("urllib.request.urlopen", fail)

    assert main(["validate", "--config", str(config)]) == 0


def test_probe_noop_without_openai_factory_models() -> None:
    registry = load_registry_text(
        'specVersion: "0.1"\n'
        "providers:\n"
        "  router:\n"
        "    name: Router\n"
        "    targets: [factory]\n"
        "    baseUrl: https://router.example/v1\n"
        "    apiKey: env.ROUTER_KEY\n"
        "    provider: generic-chat-completion-api\n"
        "    enabled: true\n"
        "    models:\n"
        "      primary:\n"
        "        displayName: Primary\n"
        "        contextWindow: 8192\n"
        "        maxOutputTokens: 1024\n"
        "        enabled: true\n"
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("generic transport must not be probed")

    monkeypatch_target = pytest.MonkeyPatch()
    monkeypatch_target.setattr("urllib.request.build_opener", fail)
    try:
        assert probe_factory_models(registry, {"ROUTER_KEY": KEY_SENTINEL}) == ()
    finally:
        monkeypatch_target.undo()


def test_probe_noop_for_anthropic_without_explicit_override() -> None:
    registry = load_registry_text(
        'specVersion: "0.1"\n'
        "providers:\n"
        "  router:\n"
        "    name: Router\n"
        "    targets: [factory]\n"
        "    baseUrl: https://api.surplusintelligence.ai/v1\n"
        "    apiKey: env.ROUTER_KEY\n"
        "    provider: anthropic\n"
        "    enabled: true\n"
        "    models:\n"
        "      claude-model:\n"
        "        displayName: Claude Model\n"
        "        contextWindow: 8192\n"
        "        maxOutputTokens: 1024\n"
        "        enabled: true\n"
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "anthropic transport without an explicit per-model baseUrl must not be probed"
        )

    monkeypatch_target = pytest.MonkeyPatch()
    monkeypatch_target.setattr("urllib.request.build_opener", fail)
    try:
        assert probe_factory_models(registry, {"ROUTER_KEY": KEY_SENTINEL}) == ()
    finally:
        monkeypatch_target.undo()


def test_probe_excludes_models_named_in_env() -> None:
    registry = load_registry_text(_openai_factory_registry("https://router.example/v1"))

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("excluded model must not be probed")

    monkeypatch_target = pytest.MonkeyPatch()
    monkeypatch_target.setattr("urllib.request.build_opener", fail)
    try:
        assert (
            probe_factory_models(
                registry,
                {"ROUTER_KEY": KEY_SENTINEL, "MODFIG_PROBE_EXCLUDE": "primary, other"},
            )
            == ()
        )
    finally:
        monkeypatch_target.undo()


# --- VAL-PROBE-002: probe is scoped to openai declarations at /responses ---


def test_probe_probes_only_openai_factory_models_at_responses() -> None:
    with _stub_server() as openai_server, _stub_server() as generic_server:
        openai_url = f"http://127.0.0.1:{openai_server.server_address[1]}/v1"
        generic_url = f"http://127.0.0.1:{generic_server.server_address[1]}/v1"
        registry = load_registry_text(_mixed_factory_registry(openai_url, generic_url))

        probed = probe_factory_models(
            registry, {"OPENAI_KEY": KEY_SENTINEL, "GENERIC_KEY": "generic-key"}
        )

    assert probed == (("openai-router", "openai-model"),)
    assert len(openai_server.requests) == 1
    assert openai_server.requests[0]["path"] == "/v1/responses"
    assert openai_server.requests[0]["auth"] == f"Bearer {KEY_SENTINEL}"
    sent = json.loads(openai_server.requests[0]["body"])
    assert sent["model"] == "openai-model"
    # generic transport is never Responses-probed
    assert generic_server.requests == []


# --- VAL-PROBE-002b: factory passthroughs do not extend probe coverage ---


def test_probe_ignores_factory_passthroughs_for_coverage() -> None:
    with _stub_server() as server:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        registry = load_registry_text(
            'specVersion: "0.1"\n'
            "providers:\n"
            "  router:\n"
            "    name: Router\n"
            "    targets: [factory]\n"
            f"    baseUrl: {url}\n"
            "    apiKey: env.ROUTER_KEY\n"
            "    provider: generic-chat-completion-api\n"
            "    enabled: true\n"
            "    models:\n"
            "      pinned:\n"
            "        displayName: Pinned\n"
            "        contextWindow: 8192\n"
            "        maxOutputTokens: 1024\n"
            "        enabled: true\n"
            "        extensions:\n"
            "          factory:\n"
            "            providers: [openai]\n"
            "            extraArgs:\n"
            "              provider: [openai]\n"
            "      args-only:\n"
            "        displayName: Args Only\n"
            "        contextWindow: 8192\n"
            "        maxOutputTokens: 1024\n"
            "        enabled: true\n"
            "        extensions:\n"
            "          factory:\n"
            "            extraArgs:\n"
            "              provider: [openai]\n"
        )

        probed = probe_factory_models(registry, {"ROUTER_KEY": KEY_SENTINEL})

    # probe scope is the effective wire declared in the registry (openai), not
    # provider pins or extraArgs passthroughs; generic transport is never
    # Responses-probed
    assert probed == ()
    assert server.requests == []


# --- VAL-PROBE-002c: anthropic models with an explicit baseUrl probe at Messages ---


def _anthropic_factory_registry(base_url: str, *, model: str = "claude-model") -> str:
    return (
        f'specVersion: "0.1"\n'
        f"providers:\n"
        f"  surplus:\n"
        f"    name: Surplus\n"
        f"    targets: [factory]\n"
        f"    baseUrl: https://api.surplusintelligence.ai/v1\n"
        f"    apiKey: env.SURPLUS_KEY\n"
        f"    provider: anthropic\n"
        f"    enabled: true\n"
        f"    models:\n"
        f"      {model}:\n"
        f"        displayName: Claude Model\n"
        f"        contextWindow: 8192\n"
        f"        maxOutputTokens: 1024\n"
        f"        baseUrl: {base_url}\n"
        f"        enabled: true\n"
    )


def test_probe_anthropic_override_hits_messages_endpoint() -> None:
    with _stub_server(
        [{"status": 200, "body": b'{"content": [{"type": "text", "text": "hi"}]}'}]
    ) as server:
        url = f"http://127.0.0.1:{server.server_address[1]}/anthropic"
        registry = load_registry_text(_anthropic_factory_registry(url))

        probed = probe_factory_models(registry, {"SURPLUS_KEY": KEY_SENTINEL})

    assert probed == (("surplus", "claude-model"),)
    assert len(server.requests) == 1
    assert server.requests[0]["path"] == "/anthropic/v1/messages"
    assert server.requests[0]["x_api_key"] == KEY_SENTINEL
    assert server.requests[0]["anthropic_version"] == "2023-06-01"
    assert server.requests[0]["auth"] == ""
    sent = json.loads(server.requests[0]["body"])
    assert sent["model"] == "claude-model"
    assert sent["max_tokens"] == 1
    assert sent["messages"] == [{"role": "user", "content": "ping"}]


def test_probe_anthropic_override_rejects_unusable_messages_output() -> None:
    with _stub_server([{"status": 200, "body": b"{}"}]) as server:
        url = f"http://127.0.0.1:{server.server_address[1]}/anthropic"
        registry = load_registry_text(_anthropic_factory_registry(url))
        with pytest.raises(AppError, match="unusable response output"):
            probe_factory_models(registry, {"SURPLUS_KEY": KEY_SENTINEL})

    assert server.requests[0]["path"] == "/anthropic/v1/messages"


def test_probe_anthropic_override_fails_closed_without_leaking_body() -> None:
    with _stub_server([{"status": 503, "body": BODY_SENTINEL}]) as server:
        url = f"http://127.0.0.1:{server.server_address[1]}/anthropic"
        registry = load_registry_text(_anthropic_factory_registry(url))
        with pytest.raises(AppError) as exc_info:
            probe_factory_models(registry, {"SURPLUS_KEY": KEY_SENTINEL})

    message = exc_info.value.message
    assert "Messages probe failed" in message
    assert "surplus" in message and "claude-model" in message
    assert "non-200" in message and "503" in message
    assert KEY_SENTINEL not in message
    assert BODY_SENTINEL.decode() not in message


# --- VAL-PROBE-003: probe success requires usable output ---


def test_probe_accepts_non_empty_output() -> None:
    with _stub_server([{"status": 200, "body": b'{"output": [{"type": "message"}]}'}]) as server:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        registry = load_registry_text(_openai_factory_registry(url))
        assert probe_factory_models(registry, {"ROUTER_KEY": KEY_SENTINEL}) == (
            ("router", "primary"),
        )


def test_probe_models_run_concurrently() -> None:
    """Two slow endpoints finish in ~one delay, not the sum (regression guard
    for sequential probing that made apply preflight exceed sync timeouts)."""
    behavior = {"status": 200, "body": b'{"output": [{"type": "message"}]}', "delay": 1.0}
    with _stub_server([behavior, behavior]) as server:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        registry = load_registry_text(
            'specVersion: "0.1"\n'
            "providers:\n"
            "  router:\n"
            "    name: Router\n"
            "    targets: [factory]\n"
            f"    baseUrl: {url}\n"
            "    apiKey: env.ROUTER_KEY\n"
            "    provider: openai\n"
            "    enabled: true\n"
            "    models:\n"
            "      one:\n"
            "        displayName: One\n"
            "        contextWindow: 8192\n"
            "        maxOutputTokens: 1024\n"
            "        enabled: true\n"
            "      two:\n"
            "        displayName: Two\n"
            "        contextWindow: 8192\n"
            "        maxOutputTokens: 1024\n"
            "        enabled: true\n"
        )
        start = time.monotonic()
        probed = probe_factory_models(registry, {"ROUTER_KEY": KEY_SENTINEL})
        elapsed = time.monotonic() - start

    assert probed == (("router", "one"), ("router", "two"))
    assert elapsed < 1.9, f"probes appear sequential: {elapsed:.2f}s for two 1s endpoints"


@pytest.mark.parametrize(
    "body",
    [b"", b"not-json", b"{}", b'{"output": []}', b'{"output": null}', b'{"output": "string"}'],
)
def test_probe_rejects_empty_or_unparsable_output(body: bytes) -> None:
    with _stub_server([{"status": 200, "body": body}]) as server:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        registry = load_registry_text(_openai_factory_registry(url))
        with pytest.raises(AppError, match="unusable response output"):
            probe_factory_models(registry, {"ROUTER_KEY": KEY_SENTINEL})


# --- VAL-PROBE-004: failures are safe and identifiable, no leaks ---


def test_probe_rejects_invalid_timeout_env() -> None:
    registry = load_registry_text(_openai_factory_registry("https://router.example/v1"))

    with pytest.raises(AppError, match="MODFIG_PROBE_TIMEOUT"):
        probe_factory_models(
            registry,
            {"ROUTER_KEY": KEY_SENTINEL, "MODFIG_PROBE_TIMEOUT": "soon"},
        )


def test_probe_rejects_non_positive_timeout_env() -> None:
    registry = load_registry_text(_openai_factory_registry("https://router.example/v1"))

    with pytest.raises(AppError, match="MODFIG_PROBE_TIMEOUT"):
        probe_factory_models(
            registry,
            {"ROUTER_KEY": KEY_SENTINEL, "MODFIG_PROBE_TIMEOUT": "0"},
        )


def test_probe_missing_secret_fails_closed_with_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = load_registry_text(_openai_factory_registry("https://router.example/v1"))

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not open a socket when the secret is missing")

    monkeypatch.setattr("urllib.request.urlopen", fail)

    with pytest.raises(AppError) as exc_info:
        probe_factory_models(registry, {})

    message = exc_info.value.message
    assert "Responses probe failed" in message
    assert "router" in message
    assert "primary" in message
    assert "ROUTER_KEY" in message  # variable name, not value, is permitted
    assert KEY_SENTINEL not in message


def test_probe_non_200_fails_closed_without_leaking_body() -> None:
    with _stub_server([{"status": 503, "body": BODY_SENTINEL}]) as server:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        registry = load_registry_text(_openai_factory_registry(url))
        with pytest.raises(AppError) as exc_info:
            probe_factory_models(registry, {"ROUTER_KEY": KEY_SENTINEL})

    message = exc_info.value.message
    assert "non-200" in message
    assert "503" in message
    assert "router" in message and "primary" in message
    assert KEY_SENTINEL not in message
    assert BODY_SENTINEL.decode() not in message


def test_probe_timeout_fails_closed() -> None:
    with _stub_server([{"status": 200, "body": b"{}", "delay": 1.0}]) as server:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        registry = load_registry_text(_openai_factory_registry(url))
        with pytest.raises(AppError, match="timed out") as exc_info:
            probe_factory_models(registry, {"ROUTER_KEY": KEY_SENTINEL}, timeout=0.3)

    assert KEY_SENTINEL not in exc_info.value.message


def test_probe_transport_error_fails_closed() -> None:
    port = _closed_loopback_port()
    registry = load_registry_text(_openai_factory_registry(f"http://127.0.0.1:{port}/v1"))
    with pytest.raises(AppError, match="transport error") as exc_info:
        probe_factory_models(registry, {"ROUTER_KEY": KEY_SENTINEL}, timeout=1.0)

    assert KEY_SENTINEL not in exc_info.value.message


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_probe_refuses_redirect_without_contacting_target(status: int) -> None:
    with _stub_server() as target:
        target_url = f"http://127.0.0.1:{target.server_address[1]}/v1/responses"
        with _stub_server(
            [{"status": status, "location": target_url, "body": BODY_SENTINEL}]
        ) as origin:
            origin_url = f"http://127.0.0.1:{origin.server_address[1]}/v1"
            registry = load_registry_text(_openai_factory_registry(origin_url))
            with pytest.raises(AppError, match=rf"redirect.*{status}") as exc_info:
                probe_factory_models(registry, {"ROUTER_KEY": KEY_SENTINEL})

    assert target.requests == []
    message = exc_info.value.message
    assert KEY_SENTINEL not in message
    assert BODY_SENTINEL.decode() not in message


@pytest.mark.parametrize(
    "failure",
    [
        http.client.BadStatusLine(f"{KEY_SENTINEL} {BODY_SENTINEL.decode()}"),
        http.client.RemoteDisconnected(f"{KEY_SENTINEL} {BODY_SENTINEL.decode()}"),
        http.client.HTTPException(f"{KEY_SENTINEL} {BODY_SENTINEL.decode()}"),
    ],
)
def test_probe_sanitizes_http_exception_without_secrets(
    monkeypatch: pytest.MonkeyPatch, failure: http.client.HTTPException
) -> None:
    registry = load_registry_text(_openai_factory_registry("http://127.0.0.1:1/v1"))

    class FailingOpener:
        def open(self, *args: object, **kwargs: object) -> object:
            raise failure

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: FailingOpener())

    with pytest.raises(AppError, match="transport error") as exc_info:
        probe_factory_models(registry, {"ROUTER_KEY": KEY_SENTINEL})

    message = exc_info.value.message
    assert "router" in message and "primary" in message
    assert KEY_SENTINEL not in message
    assert BODY_SENTINEL.decode() not in message


# --- VAL-CROSS-002: validate --adapters runs the probe; apply aborts before mutation ---


@POSIX_SECURE_IO
def test_validate_adapters_runs_probe_and_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _stub_server() as server:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        config = tmp_path / "modfig.yaml"
        _write_registry(config, _openai_factory_registry(url))
        monkeypatch.setenv("ROUTER_KEY", KEY_SENTINEL)

        assert main(["validate", "--adapters", "--config", str(config)]) == 0

    assert len(server.requests) == 1
    assert server.requests[0]["path"] == "/v1/responses"


@POSIX_SECURE_IO
def test_validate_adapters_fails_closed_when_probe_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    with _stub_server([{"status": 503, "body": BODY_SENTINEL}]) as server:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        config = tmp_path / "modfig.yaml"
        _write_registry(config, _openai_factory_registry(url))
        monkeypatch.setenv("ROUTER_KEY", KEY_SENTINEL)

        assert main(["validate", "--adapters", "--config", str(config)]) == 1

    err = capsys.readouterr().err
    assert "Responses probe failed" in err
    assert "503" in err
    assert KEY_SENTINEL not in err
    assert BODY_SENTINEL.decode() not in err


@POSIX_SECURE_IO
def test_apply_aborts_before_mutation_when_probe_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "modfig.yaml"
    _write_registry(
        config,
        'specVersion: "0.1"\n'
        "providers:\n"
        "  example:\n"
        "    name: Example\n"
        "    targets: [factory]\n"
        "    baseUrl: https://api.example.com/v1\n"
        "    apiKey: env.EXAMPLE_KEY\n"
        "    enabled: true\n"
        "    models:\n"
        "      example-model:\n"
        "        displayName: Example Model\n"
        "        contextWindow: 8192\n"
        "        maxOutputTokens: 1024\n"
        "        provider: openai\n"
        "        enabled: true\n",
    )
    manifest = tmp_path / "manifest.json"
    journal = tmp_path / "pending.json"
    backups = tmp_path / "backups"
    monkeypatch.setattr(app, "resolve_manifest_path", lambda *_: manifest)

    def failing_probe(*args: object, **kwargs: object) -> None:
        raise AppError("Responses probe failed: sentinel probe failure")

    monkeypatch.setattr(app.factory, "probe_factory_models", failing_probe)

    with pytest.raises(AppError, match="sentinel probe failure"):
        app._apply_transaction(
            str(config),
            "factory",
            True,
            {},
            journal_path=journal,
            backup_root=backups,
        )

    assert not journal.exists()
    assert not backups.exists() or list(backups.iterdir()) == []


@POSIX_SECURE_IO
def test_apply_persists_factory_providers_and_passthroughs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full transaction: extensions.factory providers/extraArgs/extraHeaders
    reach the written settings.json."""
    config = tmp_path / "modfig.yaml"
    _write_registry(
        config,
        'specVersion: "0.1"\n'
        "providers:\n"
        "  surplus:\n"
        "    name: Surplus\n"
        "    targets: [factory]\n"
        "    baseUrl: https://api.surplusintelligence.ai/v1\n"
        "    apiKey: env.SURPLUS_KEY\n"
        "    provider: generic-chat-completion-api\n"
        "    enabled: true\n"
        "    models:\n"
        "      pinned:\n"
        "        displayName: Pinned\n"
        "        contextWindow: 8192\n"
        "        maxOutputTokens: 1024\n"
        "        enabled: true\n"
        "        extensions:\n"
        "          factory:\n"
        "            providers: [openai]\n"
        "            extraArgs:\n"
        "              max_price_per_1m: 8.0\n"
        "            extraHeaders:\n"
        "              X-Pin: static\n"
        "      plain:\n"
        "        displayName: Plain\n"
        "        contextWindow: 8192\n"
        "        maxOutputTokens: 1024\n"
        "        enabled: true\n"
        "        extensions:\n"
        "          factory:\n"
        "            extraArgs: [1, two, {three: null}]\n"
        "            extraHeaders: static\n",
    )
    manifest = tmp_path / "manifest.json"
    journal = tmp_path / "pending.json"
    backups = tmp_path / "backups"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(app, "resolve_manifest_path", lambda *_: manifest)
    monkeypatch.setattr(app.factory, "probe_factory_models", lambda *args, **kwargs: ())
    settings_path = tmp_path / ".factory" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text('{"customModels": [], "modelFavorites": []}', encoding="utf-8")
    settings_path.chmod(0o600)

    app._apply_transaction(
        str(config),
        "factory",
        True,
        {},
        journal_path=journal,
        backup_root=backups,
    )

    written = json.loads((tmp_path / ".factory" / "settings.json").read_text())
    by_model = {entry["model"]: entry for entry in written["customModels"]}
    assert by_model["pinned"]["provider"] == "generic-chat-completion-api"
    assert by_model["pinned"]["extraArgs"] == {
        "max_price_per_1m": 8.0,
        "provider": ["openai"],
    }
    assert by_model["pinned"]["extraHeaders"] == {"X-Pin": "static"}
    assert by_model["plain"]["provider"] == "generic-chat-completion-api"
    # non-object passthrough shapes persist verbatim through the JSON round-trip
    assert by_model["plain"]["extraArgs"] == [1, "two", {"three": None}]
    assert by_model["plain"]["extraHeaders"] == "static"
