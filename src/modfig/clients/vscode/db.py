"""Conservative SQLite state-store helpers for stable Microsoft Code."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ...errors import AppError


@dataclass(frozen=True)
class DatabasePaths:
    database: Path
    wal: Path
    shm: Path

    def members(self) -> tuple[Path, Path, Path]:
        return (self.database, self.wal, self.shm)

    def __post_init__(self) -> None:
        expected = ("state.vscdb", "state.vscdb-wal", "state.vscdb-shm")
        actual = tuple(path.name for path in self.members())
        if actual != expected or len({path.parent for path in self.members()}) != 1:
            raise AppError("VS Code state database members are invalid")
        if any(not path.is_absolute() for path in self.members()):
            raise AppError("VS Code state database paths must be absolute")


def _consistent_backup(path: Path) -> None:
    """Ask SQLite for a consistent read before taking opaque member bytes."""
    try:
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        target = sqlite3.connect(":memory:")
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
    except sqlite3.Error as exc:
        raise AppError("VS Code state database consistent snapshot failed") from exc


def snapshot_members(paths: DatabasePaths) -> dict[Path, bytes | None]:
    """Capture exact member bytes and presence without using Code's secret store."""
    statuses: dict[Path, os.stat_result | None] = {}
    for path in paths.members():
        status: os.stat_result | None
        try:
            status = path.lstat()
        except FileNotFoundError:
            statuses[path] = None
            continue
        if not path.is_file() or path.is_symlink() or status.st_uid != os.getuid():
            raise AppError("VS Code state database member is unsafe")
        statuses[path] = status

    if statuses[paths.database] is not None:
        _consistent_backup(paths.database)

    result: dict[Path, bytes | None] = {}
    for path, status in statuses.items():
        if status is None:
            result[path] = None
            continue
        content = path.read_bytes()
        if path.stat().st_size != len(content):
            raise AppError("VS Code state database member changed while reading")
        result[path] = content
    return result


def _require_item_table(connection: sqlite3.Connection) -> None:
    table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'ItemTable'"
    ).fetchone()
    if table != ("ItemTable",):
        raise AppError("VS Code state database ItemTable contract is unavailable")
    columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(ItemTable)"))
    if columns != ("key", "value"):
        raise AppError("VS Code state database ItemTable shape is unsupported")


def _apply(connection: sqlite3.Connection, rows: Mapping[str, bytes | str | None]) -> None:
    _require_item_table(connection)
    for key, value in rows.items():
        if not isinstance(key, str) or not key:
            raise AppError("VS Code secret row key is invalid")
        if value is None:
            connection.execute("DELETE FROM ItemTable WHERE key = ?", (key,))
        elif isinstance(value, (bytes, str)):
            connection.execute(
                "INSERT INTO ItemTable(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        else:
            raise AppError("VS Code secret row value is invalid")


def plan_secret_rows_bundle(
    paths: DatabasePaths,
    rows: Mapping[str, bytes | str | None],
    *,
    snapshot: Mapping[Path, bytes | None] | None = None,
) -> dict[Path, bytes | None]:
    """Plan row changes against a private, non-WAL copy of a database bundle."""
    if snapshot is None:
        snapshot = snapshot_members(paths)
    elif set(snapshot) != set(paths.members()):
        raise AppError("VS Code state database bundle snapshot is incomplete")
    if snapshot[paths.database] is None:
        raise AppError("VS Code state database is absent or unsafe")

    with tempfile.TemporaryDirectory(prefix="modfig-vscode-") as directory:
        temporary = DatabasePaths(
            Path(directory) / "state.vscdb",
            Path(directory) / "state.vscdb-wal",
            Path(directory) / "state.vscdb-shm",
        )
        for original, temporary_member in zip(paths.members(), temporary.members(), strict=True):
            content = snapshot[original]
            if content is not None:
                temporary_member.write_bytes(content)

        connection = sqlite3.connect(f"file:{temporary.database}?mode=rw", uri=True)
        try:
            mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
            if mode != ("delete",):
                raise AppError("VS Code state database planning could not disable WAL mode")
            _apply(connection, rows)
            connection.commit()
        except AppError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise AppError("VS Code state database planning failed") from exc
        finally:
            connection.close()

        if temporary.wal.exists() or temporary.shm.exists():
            raise AppError("VS Code state database planning could not checkpoint WAL sidecars")
        return {
            paths.database: temporary.database.read_bytes(),
            paths.wal: None,
            paths.shm: None,
        }


def owned_row_ids(source: bytes) -> tuple[str, ...]:
    """List ModFig-owned stable Code secret keys without reading values."""
    if not isinstance(source, bytes):
        raise AppError("VS Code state database bytes are invalid")
    with tempfile.TemporaryDirectory(prefix="modfig-vscode-") as directory:
        path = Path(directory) / "state.vscdb"
        path.write_bytes(source)
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                _require_item_table(connection)
                return tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT key FROM ItemTable WHERE "
                        "(key GLOB 'secret://chat.lm.secret.lm-*' "
                        "OR key GLOB 'modfig:ModFig/*') ORDER BY key"
                    )
                )
            finally:
                connection.close()
        except AppError:
            raise
        except sqlite3.Error as exc:
            raise AppError("VS Code state database verification failed") from exc


def read_owned_row_values(source: bytes, keys: tuple[str, ...]) -> dict[str, bytes]:
    """Read only specified ItemTable keys from private database bytes."""
    if not isinstance(source, bytes):
        raise AppError("VS Code state database bytes are invalid")
    if len(keys) != len(set(keys)) or any(not isinstance(key, str) or not key for key in keys):
        raise AppError("VS Code secret row keys are invalid")
    with tempfile.TemporaryDirectory(prefix="modfig-vscode-") as directory:
        path = Path(directory) / "state.vscdb"
        path.write_bytes(source)
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                _require_item_table(connection)
                values: dict[str, bytes] = {}
                for key in keys:
                    row = connection.execute(
                        "SELECT value FROM ItemTable WHERE key = ?", (key,)
                    ).fetchone()
                    if row is not None:
                        value = row[0]
                        if isinstance(value, bytes):
                            values[key] = value
                        elif isinstance(value, str):
                            values[key] = value.encode("utf-8")
                        else:
                            raise AppError("VS Code secret row value is invalid")
                return values
            finally:
                connection.close()
        except AppError:
            raise
        except sqlite3.Error as exc:
            raise AppError("VS Code state database verification failed") from exc
