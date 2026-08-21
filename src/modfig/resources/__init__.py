from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def read_resource_bytes(path: str) -> bytes:
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".."} for part in parts)
    ):
        raise ValueError("unsafe resource path")
    try:
        return files(__package__).joinpath(*parts).read_bytes()
    except FileNotFoundError:
        if parts[0] != "spec":
            raise
        source_root = Path(__file__).resolve().parents[3]
        if not (source_root / "pyproject.toml").is_file():
            raise
        return (source_root / Path(*parts)).read_bytes()
