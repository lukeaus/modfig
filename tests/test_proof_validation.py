from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest

from modfig.proof import ReleaseRevisions, load_capability_matrix, validate_release_evidence

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
FIXTURE_HASH = "sha256:d607e162ff8a7f5dbdfd684e75d3a883650f5a109fee7a76adfc03caba1acfe3"
NOW = datetime(2026, 7, 27, tzinfo=UTC)
REVISIONS = ReleaseRevisions(adapter=DIGEST_A, spec=DIGEST_A, artifact=DIGEST_A)


def _supported_row() -> dict[str, Any]:
    return {
        "id": "chatgpt-macos-cli-tui-stable-shared-user-1.0",
        "logicalClient": "chatgpt",
        "component": {"kind": "core"},
        "adapterId": "modfig.chatgpt",
        "featureKey": "features.core.catalog",
        "os": "macos",
        "osBuild": "build-1",
        "surface": "cli-tui",
        "channel": "stable",
        "profileMode": "shared-user",
        "clientVersion": "1.0",
        "supportStatus": "supported",
        "freshnessDays": 30,
        "paths": ["${HOME}/.codex/config.toml"],
        "credentialMechanism": "environment-variable-reference",
        "processDetector": "chatgpt-shared-config",
        "contractFixture": {
            "path": "spec/fixtures/proof/chatgpt/chatgpt-shared-config.contract.json",
            "sha256": FIXTURE_HASH,
        },
        "requiredEvidence": [
            "authenticatedRequest",
            "restartReload",
            "foreignStatePreservation",
            "recovery",
            "secretScan",
        ],
    }


def _unsupported_unproven_row() -> dict[str, Any]:
    return {
        "id": "chatgpt-macos-cli-tui-stable-shared-user-unproven",
        "logicalClient": "chatgpt",
        "component": {"kind": "core"},
        "adapterId": "modfig.chatgpt",
        "featureKey": "features.core.catalog",
        "os": "macos",
        "osBuild": "unproven",
        "surface": "cli-tui",
        "channel": "stable",
        "profileMode": "shared-user",
        "clientVersion": "unproven",
        "supportStatus": "unsupported",
        "unsupportedKind": "unproven-contract",
        "unsupportedReason": (
            "ChatGPT shared configuration, catalog, runtime, restart, "
            "and recovery proof have not been recorded."
        ),
        "freshnessDays": None,
        "paths": [],
        "credentialMechanism": "environment-variable-reference",
        "processDetector": "unproven",
        "contractFixture": {
            "path": "spec/fixtures/proof/chatgpt/chatgpt-shared-config.contract.json",
            "sha256": FIXTURE_HASH,
        },
        "requiredEvidence": [],
    }


def _matrix(row: dict[str, Any]) -> dict[str, Any]:
    return {"matrixVersion": 1, "proofSchema": "proof-record.schema.json", "rows": [row]}


def _proof(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "proofVersion": 1,
        "rowId": row["id"],
        "identity": {
            "logicalClient": row["logicalClient"],
            "component": deepcopy(row["component"]),
            "adapterId": row["adapterId"],
            "featureKey": row["featureKey"],
            "os": {"family": row["os"], "build": row["osBuild"]},
            "surface": row["surface"],
            "channel": row["channel"],
            "profileMode": row["profileMode"],
            "client": {
                "version": row["clientVersion"],
                "build": "client-build",
                "executablePath": "${APPLICATIONS}/ChatGPT",
                "executableSha256": DIGEST_A,
                "userDataIdentity": DIGEST_A,
            },
            "owner": {"kind": "uid", "identityHash": DIGEST_A},
        },
        "paths": [{"role": "config", "path": row["paths"][0], "identityHash": DIGEST_A}],
        "credentialMechanism": row["credentialMechanism"],
        "processCheck": {
            "detectorId": row["processDetector"],
            "quiescent": True,
            "executables": [
                {"role": "writer", "path": "${APPLICATIONS}/ChatGPT", "sha256": DIGEST_A}
            ],
        },
        "harnessVersion": "1.0",
        "revisions": {"adapter": DIGEST_A, "spec": DIGEST_A, "artifact": DIGEST_A},
        "fixture": deepcopy(row["contractFixture"]),
        "procedure": {"id": "runtime-proof", "revision": DIGEST_A},
        "startedAt": "2026-07-26T00:00:00Z",
        "completedAt": "2026-07-26T00:01:00Z",
        "validUntil": "2026-08-25T00:01:00Z",
        "results": {
            name: {"status": "pass", "summary": "sanitized"} for name in row["requiredEvidence"]
        },
        "sanitization": {
            "containsCredentialValues": False,
            "containsEnvironment": False,
            "containsFullArgv": False,
            "containsRawClientState": False,
        },
    }


def test_empty_evidence_validates_current_unsupported_matrix() -> None:
    validate_release_evidence([], now=NOW, revisions=REVISIONS)


def test_proof_for_current_unsupported_row_is_rejected() -> None:
    row = load_capability_matrix()["rows"][0]
    proof = _proof({**_supported_row(), **row, "paths": ["${HOME}/settings.json"]})

    with pytest.raises(ValueError, match="unsupported row"):
        validate_release_evidence([proof], now=NOW, revisions=REVISIONS)


def test_supported_row_accepts_fresh_matching_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _supported_row()
    monkeypatch.setattr("modfig.proof.load_capability_matrix", lambda: _matrix(row))

    validate_release_evidence([_proof(row)], now=NOW, revisions=REVISIONS)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda proof: proof["identity"].update(surface="desktop"), "identity"),
        (lambda proof: proof["revisions"].update(artifact=DIGEST_B), "revisions"),
        (lambda proof: proof["fixture"].update(sha256=DIGEST_B), "contract"),
        (lambda proof: proof.update(validUntil="2026-07-26T00:01:00Z"), "timestamps"),
        (
            lambda proof: proof["results"]["secretScan"].update(status="fail"),
            "failed result",
        ),
    ],
)
def test_supported_row_rejects_mismatched_or_failed_evidence(
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
    message: str,
) -> None:
    row = _supported_row()
    proof = _proof(row)
    mutate(proof)
    monkeypatch.setattr("modfig.proof.load_capability_matrix", lambda: _matrix(row))

    with pytest.raises(ValueError, match=message):
        validate_release_evidence([proof], now=NOW, revisions=REVISIONS)


def test_supported_row_rejects_missing_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _supported_row()
    monkeypatch.setattr("modfig.proof.load_capability_matrix", lambda: _matrix(row))

    with pytest.raises(ValueError, match="missing proof"):
        validate_release_evidence([], now=NOW, revisions=REVISIONS)


@pytest.mark.parametrize(
    "matrix",
    [
        {"matrixVersion": 2, "proofSchema": "proof-record.schema.json", "rows": [_supported_row()]},
        _matrix({**_supported_row(), "supportStatus": "unknown"}),
        _matrix({**_supported_row(), "freshnessDays": None}),
        {
            "matrixVersion": 1,
            "proofSchema": "proof-record.schema.json",
            "rows": [_supported_row(), _supported_row()],
        },
    ],
)
def test_malformed_capability_matrix_is_rejected(
    monkeypatch: pytest.MonkeyPatch, matrix: dict[str, Any]
) -> None:
    monkeypatch.setattr("modfig.proof.load_capability_matrix", lambda: matrix)

    with pytest.raises(ValueError, match="capability|support status"):
        validate_release_evidence([], now=NOW, revisions=REVISIONS)


def test_unknown_proof_row_is_rejected() -> None:
    proof = _proof(_supported_row())

    with pytest.raises(ValueError, match="unknown capability row"):
        validate_release_evidence([proof], now=NOW, revisions=REVISIONS)


@pytest.mark.parametrize("forbidden_key", ["environment", "rawClientState", "databaseBytes"])
def test_supported_row_rejects_forbidden_raw_state_recursively(
    monkeypatch: pytest.MonkeyPatch, forbidden_key: str
) -> None:
    row = _supported_row()
    proof = _proof(row)
    proof["results"]["secretScan"]["nested"] = {forbidden_key: "redacted"}
    monkeypatch.setattr("modfig.proof.load_capability_matrix", lambda: _matrix(row))

    with pytest.raises(ValueError, match="forbidden raw state"):
        validate_release_evidence([proof], now=NOW, revisions=REVISIONS)


def test_secret_sentinel_is_rejected_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _supported_row()
    proof = _proof(row)
    secret = "TOP-SECRET-SENTINEL"
    proof["results"]["secretScan"]["summary"] = f"leaked {secret}"
    monkeypatch.setattr("modfig.proof.load_capability_matrix", lambda: _matrix(row))

    with pytest.raises(ValueError, match="secret sentinel") as raised:
        validate_release_evidence([proof], now=NOW, revisions=REVISIONS, secret_sentinels=[secret])

    assert secret not in str(raised.value)


def test_supported_row_rejects_non_quiescent_process_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _supported_row()
    proof = _proof(row)
    proof["processCheck"]["quiescent"] = False
    monkeypatch.setattr("modfig.proof.load_capability_matrix", lambda: _matrix(row))

    with pytest.raises(ValueError, match="quiescent"):
        validate_release_evidence([proof], now=NOW, revisions=REVISIONS)


def test_supported_rows_differing_only_by_component_or_feature_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row_a = _supported_row()
    row_b = {
        **row_a,
        "id": "chatgpt-macos-cli-tui-extension-1.0",
        "component": {"kind": "extension", "name": "catalog"},
    }
    row_c = {
        **row_a,
        "id": "chatgpt-macos-cli-tui-feature-1.0",
        "featureKey": "features.core.models",
    }
    monkeypatch.setattr(
        "modfig.proof.load_capability_matrix",
        lambda: {
            "matrixVersion": 1,
            "proofSchema": "proof-record.schema.json",
            "rows": [row_a, row_b, row_c],
        },
    )

    validate_release_evidence(
        [_proof(row_a), _proof(row_b), _proof(row_c)], now=NOW, revisions=REVISIONS
    )


def test_supported_row_rejects_invalid_logical_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {**_supported_row(), "logicalClient": "Bad_Client"}
    monkeypatch.setattr("modfig.proof.load_capability_matrix", lambda: _matrix(row))

    with pytest.raises(ValueError, match="capability|logical client"):
        validate_release_evidence([], now=NOW, revisions=REVISIONS)


def test_two_supported_rows_differing_only_by_os_build_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row_a = _supported_row()
    row_b = {
        **row_a,
        "id": "chatgpt-macos-cli-tui-stable-shared-user-1.0-build-2",
        "osBuild": "build-2",
    }
    matrix = {
        "matrixVersion": 1,
        "proofSchema": "proof-record.schema.json",
        "rows": [row_a, row_b],
    }
    monkeypatch.setattr("modfig.proof.load_capability_matrix", lambda: matrix)

    validate_release_evidence([_proof(row_a), _proof(row_b)], now=NOW, revisions=REVISIONS)


def test_supported_row_with_stale_matrix_fixture_hash_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _supported_row()
    row["contractFixture"]["sha256"] = "sha256:" + "0" * 64
    monkeypatch.setattr("modfig.proof.load_capability_matrix", lambda: _matrix(row))

    with pytest.raises(ValueError, match="fixture hash"):
        validate_release_evidence([_proof(row)], now=NOW, revisions=REVISIONS)


def test_unsupported_unproven_row_with_stale_matrix_fixture_hash_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _unsupported_unproven_row()
    row["contractFixture"]["sha256"] = "sha256:" + "0" * 64
    monkeypatch.setattr("modfig.proof.load_capability_matrix", lambda: _matrix(row))

    with pytest.raises(ValueError, match="fixture hash"):
        validate_release_evidence([], now=NOW, revisions=REVISIONS)
