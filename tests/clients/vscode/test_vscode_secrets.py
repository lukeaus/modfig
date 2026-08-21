from __future__ import annotations

import json
import os

import pytest

from modfig.clients.vscode.secrets import (
    SecretContract,
    decode_secret,
    encode_secret,
)
from modfig.errors import AppError


class KeyBackend:
    def __init__(self, key: bytes) -> None:
        self.key = key
        self.calls = 0

    def key_bytes(self) -> bytes:
        self.calls += 1
        return self.key


def test_macos_oscrypt_v10_round_trip_uses_backend_and_prefix() -> None:
    backend = KeyBackend(b"Code Safe Storage password")
    contract = SecretContract("macos", "stable", "oscrypt-v10")

    encoded = encode_secret(b"secret-value", contract, backend)

    assert isinstance(encoded, str)
    serialized = json.loads(encoded)
    assert serialized["type"] == "Buffer"
    assert bytes(serialized["data"]).startswith(b"v10")
    assert decode_secret(encoded, contract, backend) == b"secret-value"
    assert backend.calls == 2
    assert "secret-value" not in encoded


def test_linux_oscrypt_v11_round_trip() -> None:
    backend = KeyBackend(b"secret-service-value")
    contract = SecretContract("linux", "stable", "oscrypt-v11")

    encoded = encode_secret(b"secret-value", contract, backend)

    assert encoded.startswith(b"v11")
    assert decode_secret(encoded, contract, backend) == b"secret-value"


def test_linux_basic_text_is_explicit_and_does_not_use_backend() -> None:
    backend = KeyBackend(b"unused")
    contract = SecretContract("linux", "stable", "basic-text")

    encoded = encode_secret(b"secret-value", contract, backend)

    assert encoded == b"secret-value"
    assert decode_secret(encoded, contract, backend) == b"secret-value"
    assert backend.calls == 0


def test_secret_contract_rejects_wrong_platform_format_and_tampering() -> None:
    backend = KeyBackend(os.urandom(32))
    with pytest.raises(AppError, match="format|platform|macOS"):
        encode_secret(b"x", SecretContract("macos", "stable", "oscrypt-v11"), backend)
    encoded = encode_secret(b"x", SecretContract("macos", "stable", "oscrypt-v10"), backend)
    serialized = json.loads(encoded)
    serialized["data"][-1] ^= 1
    corrupted = json.dumps(serialized)
    with pytest.raises(AppError, match="decrypt|authentication|ciphertext"):
        decode_secret(corrupted, SecretContract("macos", "stable", "oscrypt-v10"), backend)
