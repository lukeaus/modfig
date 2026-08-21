from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

import pytest

from modfig.errors import AppError
from modfig.journal import (
    InvocationJournal,
    TransactionArtifact,
    journal_bytes,
    validate_journal_bytes,
)
from modfig.manifest import (
    AdapterProvenance,
    ClientOwnership,
    ComponentOwnership,
    OwnershipManifest,
    ownership_manifest_bytes,
)
from modfig.storage import FileVersion


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_plaintext_journal_is_versioned_and_structurally_bound(tmp_path: Path) -> None:
    before = b"before"
    after = b"after"
    destination = (tmp_path / "settings.json").absolute()
    before_version = FileVersion((1, 2), (3, 4), len(before), _sha(before))
    artifact = TransactionArtifact(
        "factory",
        "core",
        "builtin.factory",
        "settings",
        PurePosixPath("settings.json"),
        destination,
        before_version,
        _sha(after),
    )
    before_manifest = ownership_manifest_bytes(OwnershipManifest())
    record = ComponentOwnership(
        "core",
        AdapterProvenance("builtin.factory", "modfig", "0.1"),
        "settings",
        PurePosixPath("settings.json"),
        _sha(before),
        _sha(after),
        {},
    )
    after_manifest = ownership_manifest_bytes(
        OwnershipManifest(clients={"factory": ClientOwnership((record,))})
    )
    journal = InvocationJournal(
        "invoke-1",
        (tmp_path / "manifest.json").absolute(),
        before_manifest,
        FileVersion((5, 6), (7, 8), len(before_manifest), _sha(before_manifest)),
        after_manifest,
        _sha(after_manifest),
        (artifact,),
        "set-1",
        "a" * 64,
    )
    encoded = journal_bytes(journal)
    assert '"journalVersion":4' in encoded.decode()
    assert validate_journal_bytes(encoded) == journal


def test_plaintext_journal_rejects_legacy_version(tmp_path: Path) -> None:
    try:
        validate_journal_bytes(b'{"journalVersion":3}')
    except Exception as exc:
        assert "version" in str(exc) or "envelope" in str(exc)
    else:
        raise AssertionError("legacy journal accepted")


def _valid_journal_bytes(tmp_path: Path) -> bytes:
    before = b"before"
    after = b"after"
    destination = (tmp_path / "settings.json").absolute()
    artifact = TransactionArtifact(
        "factory",
        "core",
        "builtin.factory",
        "settings",
        PurePosixPath("settings.json"),
        destination,
        FileVersion((1, 2), (3, 4), len(before), _sha(before)),
        _sha(after),
    )
    before_manifest = ownership_manifest_bytes(OwnershipManifest())
    record = ComponentOwnership(
        "core",
        AdapterProvenance("builtin.factory", "modfig", "0.1"),
        "settings",
        PurePosixPath("settings.json"),
        _sha(before),
        _sha(after),
        {},
    )
    after_manifest = ownership_manifest_bytes(
        OwnershipManifest(clients={"factory": ClientOwnership((record,))})
    )
    return journal_bytes(
        InvocationJournal(
            "invoke-1",
            (tmp_path / "manifest.json").absolute(),
            before_manifest,
            FileVersion((5, 6), (7, 8), len(before_manifest), _sha(before_manifest)),
            after_manifest,
            _sha(after_manifest),
            (artifact,),
            "set-1",
            "a" * 64,
        )
    )


def _reseal(payload: dict[str, object]) -> bytes:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return (
        json.dumps(
            {"journalVersion": 4, "payload": payload, "integrity": _sha(encoded)},
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def test_plaintext_journal_rejects_unknown_field(tmp_path: Path) -> None:
    raw = json.loads(_valid_journal_bytes(tmp_path))
    payload = dict(raw["payload"])
    payload["unexpectedField"] = True

    with pytest.raises(AppError, match="invalid pending journal payload"):
        validate_journal_bytes(_reseal(payload))


def test_plaintext_journal_rejects_duplicate_artifact(tmp_path: Path) -> None:
    raw = json.loads(_valid_journal_bytes(tmp_path))
    payload = dict(raw["payload"])
    payload["artifacts"] = list(payload["artifacts"]) + [dict(payload["artifacts"][0])]

    with pytest.raises(AppError, match="must be unique"):
        validate_journal_bytes(_reseal(payload))


def test_plaintext_journal_rejects_inconsistent_manifest_intent(tmp_path: Path) -> None:
    raw = json.loads(_valid_journal_bytes(tmp_path))
    payload = dict(raw["payload"])
    artifact = dict(payload["artifacts"][0])
    artifact["afterSha256"] = "b" * 64
    payload["artifacts"] = [artifact]

    with pytest.raises(AppError, match="does not match after manifest intent"):
        validate_journal_bytes(_reseal(payload))


def test_plaintext_journal_rejects_integrity_mismatch(tmp_path: Path) -> None:
    raw = json.loads(_valid_journal_bytes(tmp_path))
    raw["integrity"] = "0" * 64

    with pytest.raises(AppError, match="structural digest is corrupt"):
        validate_journal_bytes(
            (json.dumps(raw, separators=(",", ":"), sort_keys=True) + "\n").encode()
        )
