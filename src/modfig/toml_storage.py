from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.toml_document import TOMLDocument

from .errors import AppError
from .storage import read_private_bytes


class TomlStorageError(AppError):
    """A private TOML file could not be decoded or parsed safely."""


@dataclass
class ParsedToml:
    source: bytes
    document: TOMLDocument


def load_toml(path: Path) -> ParsedToml:
    source = read_private_bytes(path, "ChatGPT config")
    try:
        text = source.decode("utf-8")
        return ParsedToml(source, tomlkit.parse(text))
    except (UnicodeError, TOMLKitError) as exc:
        raise TomlStorageError(f"cannot parse ChatGPT config {path}") from exc


def dump_toml(parsed: ParsedToml) -> bytes:
    rendered = tomlkit.dumps(parsed.document).encode("utf-8")
    return parsed.source if rendered == parsed.source else rendered
