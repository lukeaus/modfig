from __future__ import annotations

import os
from pathlib import Path

import pytest
from tomlkit import table

from modfig.errors import AppError
from modfig.toml_storage import TomlStorageError, dump_toml, load_toml

pytestmark = pytest.mark.skipif(os.name == "nt", reason="requires native POSIX secure I/O")

CONFIG = b"""# keep this comment
model = "active-model"
model_provider = "foreign"
unknown = "keep"

[model_providers.foreign]
name = "Foreign"
base_url = "https://foreign.example/v1"
wire_api = "chat"

[unrelated]
values = [1, 2, 3]
"""


def private_file(path: Path, content: bytes = CONFIG) -> None:
    path.write_bytes(content)
    path.chmod(0o600)


def test_load_and_unchanged_dump_preserve_exact_toml_bytes(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    private_file(path)

    parsed = load_toml(path)

    assert parsed.source == CONFIG
    assert dump_toml(parsed) == CONFIG


def test_changed_dump_preserves_comments_unknown_tables_and_active_selection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    private_file(path)
    parsed = load_toml(path)
    provider = table()
    provider.add("name", "Managed")
    provider.add("base_url", "https://managed.example/v1")
    provider.add("env_key", "MANAGED_KEY")
    provider.add("wire_api", "responses")
    parsed.document["model_providers"]["modfig-managed"] = provider

    rendered = dump_toml(parsed)

    assert b"# keep this comment" in rendered
    assert b'unknown = "keep"' in rendered
    assert b"values = [1, 2, 3]" in rendered
    assert b'model = "active-model"' in rendered
    assert b'model_provider = "foreign"' in rendered
    assert b"[model_providers.modfig-managed]" in rendered


def test_load_rejects_malformed_toml_without_echoing_content(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    secret = "do-not-leak-sentinel"
    private_file(path, f'api_key = "{secret}"\ninvalid = [\n'.encode())

    with pytest.raises(TomlStorageError) as caught:
        load_toml(path)

    assert secret not in str(caught.value)


def test_load_rejects_symlinked_config(tmp_path: Path) -> None:
    target = tmp_path / "target.toml"
    private_file(target)
    path = tmp_path / "config.toml"
    path.symlink_to(target)

    with pytest.raises(AppError, match="must not be a symlink"):
        load_toml(path)


def test_load_rejects_unsafe_parent(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    private = unsafe / "private"
    private.mkdir(parents=True)
    unsafe.chmod(0o777)
    private.chmod(0o700)
    path = private / "config.toml"
    private_file(path)

    with pytest.raises(AppError, match="ancestor.*writable|unsafe ancestor"):
        load_toml(path)
