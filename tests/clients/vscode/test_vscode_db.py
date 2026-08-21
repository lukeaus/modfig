from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from modfig.clients.vscode.db import (
    DatabasePaths,
    plan_secret_rows_bundle,
    snapshot_members,
)
from modfig.errors import AppError


def make_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
    connection.execute("INSERT INTO ItemTable VALUES (?, ?)", ("foreign", b"foreign"))
    connection.execute("INSERT INTO ItemTable VALUES (?, ?)", ("owned", b"old"))
    connection.commit()
    connection.close()


def test_plan_secret_rows_bundle_preserves_foreign_rows(tmp_path: Path) -> None:
    db = tmp_path / "state.vscdb"
    make_db(db)
    paths = DatabasePaths(db, tmp_path / "state.vscdb-wal", tmp_path / "state.vscdb-shm")
    planned = plan_secret_rows_bundle(paths, {"owned": b"new"})
    assert planned[paths.database] is not None
    planned_db = tmp_path / "planned.vscdb"
    planned_db.write_bytes(planned[paths.database])
    connection = sqlite3.connect(planned_db)
    rows = dict(connection.execute("SELECT key, value FROM ItemTable"))
    connection.close()
    assert rows == {"foreign": b"foreign", "owned": b"new"}
    assert planned[paths.wal] is None
    assert planned[paths.shm] is None


def test_plan_secret_rows_bundle_writes_code_text_values(tmp_path: Path) -> None:
    db = tmp_path / "state.vscdb"
    make_db(db)
    paths = DatabasePaths(db, tmp_path / "state.vscdb-wal", tmp_path / "state.vscdb-shm")
    encoded = '{"type": "Buffer", "data": [118, 49, 48]}'

    planned = plan_secret_rows_bundle(paths, {"owned": encoded})

    assert planned[paths.database] is not None
    planned_db = tmp_path / "planned-text.vscdb"
    planned_db.write_bytes(planned[paths.database])
    connection = sqlite3.connect(planned_db)
    row = connection.execute(
        "SELECT typeof(value), value FROM ItemTable WHERE key = ?", ("owned",)
    ).fetchone()
    connection.close()
    assert row == ("text", encoded)


def test_read_owned_row_values_normalizes_code_text_values(tmp_path: Path) -> None:
    from modfig.clients.vscode.db import read_owned_row_values

    db = tmp_path / "state.vscdb"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
    encoded = '{"type": "Buffer", "data": [118, 49, 48]}'
    connection.execute("INSERT INTO ItemTable VALUES (?, ?)", ("owned", encoded))
    connection.commit()
    connection.close()

    assert read_owned_row_values(db.read_bytes(), ("owned",)) == {"owned": encoded.encode()}


def test_plan_secret_rows_bundle_checkpoints_present_wal_sidecars(tmp_path: Path) -> None:
    db = tmp_path / "state.vscdb"
    connection = sqlite3.connect(db)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
    connection.execute("INSERT INTO ItemTable VALUES (?, ?)", ("foreign", b"foreign"))
    connection.commit()
    wal = tmp_path / "state.vscdb-wal"
    shm = tmp_path / "state.vscdb-shm"
    assert wal.exists() and shm.exists()
    paths = DatabasePaths(db, wal, shm)
    snapshot = snapshot_members(paths)
    assert snapshot[wal] is not None and snapshot[shm] is not None
    planned = plan_secret_rows_bundle(paths, {"owned": b"new"}, snapshot=snapshot)
    connection.close()
    assert planned[wal] is None
    assert planned[shm] is None
    planned_db = tmp_path / "planned-wal.vscdb"
    assert planned[db] is not None
    planned_db.write_bytes(planned[db])
    connection = sqlite3.connect(planned_db)
    rows = dict(connection.execute("SELECT key, value FROM ItemTable"))
    connection.close()
    assert rows == {"foreign": b"foreign", "owned": b"new"}


def test_plan_secret_rows_bundle_rejects_unsafe_sidecar(tmp_path: Path) -> None:
    db = tmp_path / "state.vscdb"
    make_db(db)
    paths = DatabasePaths(db, tmp_path / "state.vscdb-wal", tmp_path / "state.vscdb-shm")
    paths.wal.symlink_to(db)

    with pytest.raises(AppError, match="unsafe"):
        plan_secret_rows_bundle(paths, {"owned": b"new"})


def test_snapshot_members_preserves_absent_sidecars(tmp_path: Path) -> None:
    db = tmp_path / "state.vscdb"
    make_db(db)
    paths = DatabasePaths(db, tmp_path / "state.vscdb-wal", tmp_path / "state.vscdb-shm")

    snapshot = snapshot_members(paths)

    assert snapshot[db] == db.read_bytes()
    assert snapshot[paths.wal] is None
    assert snapshot[paths.shm] is None
