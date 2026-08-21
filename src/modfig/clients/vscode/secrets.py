"""Versioned secret-row codecs for stable Microsoft Code.

The transaction layer never imports this module. These codecs are only for the
secret value stored by Code itself; transaction snapshots remain opaque plaintext
files in the host's private state directory.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, TypeAlias

from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from ...errors import AppError

_SALT = b"saltysalt"
_IV = b" " * 16
EncodedSecret: TypeAlias = bytes | str


class SecretKeyBackend(Protocol):
    def key_bytes(self) -> bytes: ...


@dataclass(frozen=True)
class SecretContract:
    """The proof-selected Code channel and its stored-value format."""

    os_name: str
    channel: str
    format: str

    def __post_init__(self) -> None:
        if self.os_name not in {"macos", "linux"}:
            raise AppError("VS Code secret contract supports macOS and Linux only")
        if self.channel != "stable":
            raise AppError("VS Code secret contract supports stable Code only")
        if self.format not in {"oscrypt-v10", "oscrypt-v11", "basic-text"}:
            raise AppError("VS Code secret contract format is unsupported")
        if self.format == "basic-text" and self.os_name != "linux":
            raise AppError("basic-text secret format is Linux-only")
        if self.os_name == "macos" and self.format != "oscrypt-v10":
            raise AppError("macOS stable Code requires OSCrypt v10")
        if self.os_name == "linux" and self.format not in {"oscrypt-v11", "basic-text"}:
            raise AppError("Linux stable Code requires OSCrypt v11 or basic-text")

    @property
    def prefix(self) -> bytes:
        return {"oscrypt-v10": b"v10", "oscrypt-v11": b"v11", "basic-text": b""}[self.format]

    @property
    def iterations(self) -> int:
        return {"oscrypt-v10": 1003, "oscrypt-v11": 1, "basic-text": 0}[self.format]


@dataclass(frozen=True)
class MacOSKeychainBackend:
    """Read Code's own safe-storage item, never transaction state."""

    service: str = "Code Safe Storage"
    account: str | None = None

    def key_bytes(self) -> bytes:
        command = ["security", "find-generic-password", "-s", self.service, "-w"]
        if self.account is not None:
            command[3:3] = ["-a", self.account]
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=False)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise AppError("VS Code macOS safe-storage lookup failed") from exc
        value = result.stdout.rstrip(b"\r\n")
        if not value:
            raise AppError("VS Code macOS safe-storage item is empty")
        return value


@dataclass(frozen=True)
class LinuxSecretServiceBackend:
    """Read Code's own Secret Service item, never transaction state."""

    attributes: Mapping[str, str]

    def key_bytes(self) -> bytes:
        command = ["secret-tool", "lookup"]
        for key, value in self.attributes.items():
            if not key or not value:
                raise AppError("VS Code Secret Service attributes must be non-empty")
            command.extend((key, value))
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=False)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise AppError("VS Code Linux safe-storage lookup failed") from exc
        output: bytes = result.stdout.rstrip(b"\r\n")
        if not output:
            raise AppError("VS Code Linux safe-storage item is empty")
        return output


def _key_bytes(backend: SecretKeyBackend | Callable[[], bytes]) -> bytes:
    try:
        value = backend() if callable(backend) else backend.key_bytes()
    except AppError:
        raise
    except Exception as exc:
        raise AppError("VS Code safe-storage key lookup failed") from exc
    if not isinstance(value, bytes) or not value:
        raise AppError("VS Code safe-storage key is invalid")
    return value


def _derive_key(password: bytes, contract: SecretContract) -> bytes:
    if contract.format == "basic-text":
        raise AppError("basic-text does not derive an encryption key")
    return PBKDF2HMAC(
        algorithm=hashes.SHA1(), length=16, salt=_SALT, iterations=contract.iterations
    ).derive(password)


def _encrypt(plaintext: bytes, key: bytes) -> bytes:
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(_IV)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _decrypt(ciphertext: bytes, key: bytes) -> bytes:
    if not ciphertext or len(ciphertext) % 16:
        raise AppError("VS Code secret ciphertext is truncated")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(_IV)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    try:
        unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise AppError("VS Code secret ciphertext failed authentication") from exc


def encode_secret(
    plaintext: bytes, contract: SecretContract, backend: SecretKeyBackend | Callable[[], bytes]
) -> EncodedSecret:
    if not isinstance(plaintext, bytes):
        raise AppError("VS Code secret plaintext must be bytes")
    if contract.format == "basic-text":
        try:
            plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AppError("VS Code basic-text secret must be UTF-8") from exc
        return plaintext
    encoded = contract.prefix + _encrypt(plaintext, _derive_key(_key_bytes(backend), contract))
    if contract.format == "oscrypt-v10":
        return json.dumps({"type": "Buffer", "data": list(encoded)})
    return encoded


def decode_secret(
    encoded: EncodedSecret,
    contract: SecretContract,
    backend: SecretKeyBackend | Callable[[], bytes],
) -> bytes:
    if isinstance(encoded, str) or (isinstance(encoded, bytes) and encoded.startswith(b"{")):
        try:
            value = json.loads(encoded)
        except (UnicodeError, json.JSONDecodeError):
            raise AppError("VS Code secret ciphertext is malformed") from None
        if (
            not isinstance(value, Mapping)
            or value.get("type") != "Buffer"
            or not isinstance(value.get("data"), list)
            or not all(type(item) is int and 0 <= item <= 255 for item in value["data"])
        ):
            raise AppError("VS Code secret ciphertext is malformed")
        encoded_bytes = bytes(value["data"])
    elif isinstance(encoded, bytes):
        encoded_bytes = encoded
    else:
        raise AppError("VS Code secret ciphertext must be bytes or text")
    if contract.format == "basic-text":
        try:
            encoded_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AppError("VS Code basic-text secret must be UTF-8") from exc
        return encoded_bytes
    if not encoded_bytes.startswith(contract.prefix):
        raise AppError("VS Code secret ciphertext has an unexpected version prefix")
    try:
        return _decrypt(
            encoded_bytes[len(contract.prefix) :], _derive_key(_key_bytes(backend), contract)
        )
    except AppError:
        raise
    except Exception as exc:
        raise AppError("VS Code secret ciphertext failed authentication") from exc
