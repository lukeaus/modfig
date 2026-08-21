from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

SPEC_DIR = Path(__file__).resolve().parent.parent / "spec"
MATRIX_PATH = SPEC_DIR / "capability-matrix.json"
PROOF_SCHEMA_PATH = SPEC_DIR / "proof-record.schema.json"
PROOF_FIXTURE_DIR = SPEC_DIR / "fixtures" / "proof"

EXPECTED_SURFACES = {
    ("vscode", os_name, "custom-endpoint") for os_name in ("macos", "linux", "windows")
} | {
    ("chatgpt", os_name, surface)
    for os_name in ("macos", "linux", "windows")
    for surface in ("cli-tui", "desktop")
}

FORBIDDEN_KEYS = {
    "apiKey",
    "api_key",
    "argv",
    "credentialValue",
    "databaseBytes",
    "environment",
    "rawClientState",
    "shmBytes",
    "walBytes",
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_matrix() -> dict[str, Any]:
    return _load_json(MATRIX_PATH)


def _rows_by_key(matrix: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    return {(row["logicalClient"], row["os"], row["surface"]): row for row in matrix["rows"]}


def _assert_sanitized(value: Any) -> None:
    if isinstance(value, dict):
        assert not (FORBIDDEN_KEYS & value.keys())
        for nested_value in value.values():
            _assert_sanitized(nested_value)
    elif isinstance(value, list):
        for item in value:
            _assert_sanitized(item)
    elif isinstance(value, str):
        assert "MODFIG_TEST_SECRET" not in value


def _valid_proof_record() -> dict[str, Any]:
    digest = "sha256:" + "a" * 64
    return {
        "proofVersion": 1,
        "rowId": "chatgpt-macos-cli-tui-stable-shared-user-0.1",
        "identity": {
            "logicalClient": "chatgpt",
            "component": {"kind": "core"},
            "adapterId": "modfig.chatgpt",
            "featureKey": "features.core.catalog",
            "os": {"family": "macos", "build": "sanitized-build"},
            "surface": "cli-tui",
            "channel": "stable",
            "profileMode": "shared-user",
            "client": {
                "version": "0.1",
                "build": "sanitized-build",
                "executablePath": "${APPLICATIONS}/ChatGPT",
                "executableSha256": digest,
                "userDataIdentity": digest,
            },
            "owner": {"kind": "uid", "identityHash": digest},
        },
        "paths": [
            {
                "role": "config-root",
                "path": "${HOME}/.codex",
                "identityHash": digest,
            }
        ],
        "credentialMechanism": "environment-variable-reference",
        "processCheck": {
            "detectorId": "chatgpt-shared-config",
            "quiescent": True,
            "executables": [
                {
                    "role": "writer",
                    "path": "${APPLICATIONS}/ChatGPT",
                    "sha256": digest,
                }
            ],
        },
        "harnessVersion": "0.1",
        "revisions": {"adapter": digest, "spec": digest, "artifact": digest},
        "fixture": {
            "path": "spec/fixtures/proof/chatgpt/chatgpt-shared-config.contract.json",
            "sha256": digest,
        },
        "procedure": {"id": "chatgpt-runtime-proof", "revision": digest},
        "startedAt": "2026-07-26T00:00:00Z",
        "completedAt": "2026-07-26T00:01:00Z",
        "validUntil": "2026-08-25T00:01:00Z",
        "results": {
            "authenticatedRequest": {"status": "pass", "summary": "sanitized"},
            "restartReload": {"status": "pass", "summary": "sanitized"},
            "foreignStatePreservation": {"status": "pass", "summary": "sanitized"},
            "recovery": {"status": "pass", "summary": "sanitized"},
            "secretScan": {"status": "pass", "summary": "sanitized"},
        },
        "sanitization": {
            "containsCredentialValues": False,
            "containsEnvironment": False,
            "containsFullArgv": False,
            "containsRawClientState": False,
        },
    }


def test_capability_matrix_covers_requested_target_os_surfaces() -> None:
    matrix = _load_matrix()

    assert matrix["matrixVersion"] == 1
    assert matrix["proofSchema"] == "proof-record.schema.json"
    assert set(_rows_by_key(matrix)) == EXPECTED_SURFACES


def test_capability_matrix_rows_are_unique_and_fail_closed_without_runtime_evidence() -> None:
    matrix = _load_matrix()
    rows = matrix["rows"]
    composite_keys = [
        (
            row["logicalClient"],
            json.dumps(row["component"], sort_keys=True),
            row["adapterId"],
            row["featureKey"],
            row["os"],
            row["surface"],
            row["channel"],
            row["profileMode"],
            row["clientVersion"],
        )
        for row in rows
    ]

    assert len(composite_keys) == len(set(composite_keys))
    for row in rows:
        assert row["supportStatus"] == "unsupported"
        assert row["unsupportedReason"]
        assert row["freshnessDays"] is None
        assert row["requiredEvidence"] == []


def test_linux_chatgpt_desktop_is_explicitly_not_applicable() -> None:
    row = _rows_by_key(_load_matrix())[("chatgpt", "linux", "desktop")]

    assert row["supportStatus"] == "unsupported"
    assert row["unsupportedKind"] == "not-applicable"
    assert "no Linux desktop" in row["unsupportedReason"]


def test_contract_fixture_hashes_match_matrix_references() -> None:
    matrix = _load_matrix()

    for row in matrix["rows"]:
        contract_fixture = row.get("contractFixture")
        if contract_fixture is None:
            continue
        fixture_path = SPEC_DIR.parent / contract_fixture["path"]
        expected_digest = "sha256:" + hashlib.sha256(fixture_path.read_bytes()).hexdigest()
        assert contract_fixture["sha256"] == expected_digest


def test_discovery_contract_fixtures_are_sanitized() -> None:
    fixture_paths = sorted(PROOF_FIXTURE_DIR.rglob("*.json"))

    assert fixture_paths
    for fixture_path in fixture_paths:
        _assert_sanitized(_load_json(fixture_path))


def test_chatgpt_discovery_contract_defines_fail_closed_profile_override_and_catalog_state() -> (
    None
):
    contract = _load_json(PROOF_FIXTURE_DIR / "chatgpt" / "chatgpt-shared-config.contract.json")

    assert contract["configRootResolution"]["environmentVariable"] == "CODEX_HOME"
    assert contract["surface"] == "cli-tui"
    assert contract["profileConfigPathTemplate"].endswith("/<provider-key>.config.toml")
    assert contract["profileSelection"] == {
        "namePattern": "<provider-key>",
        "mechanism": "codex --profile <provider-key>",
        "status": "required",
    }
    assert contract["selectedProfileInspection"] == "read-only"
    assert contract["managedOverrideBehavior"] == "fail-closed"
    assert contract["wireApi"] == "responses"
    assert contract["credentialField"] == "env_key"
    assert contract["catalog"] == {
        "status": "proven",
        "pointerKey": "model_catalog_json",
        "managedPathTemplate": "${CODEX_HOME:-${HOME}/.codex}/modfig-<provider-key>-catalog.json",
        "visibility": "list",
        "selectionSemantics": "replaces-picker-list",
        "staleRemoval": "sha256-ownership-reconciliation",
        "basePointer": "managed-root-key-only",
    }


def test_vscode_discovery_contract_is_limited_to_named_database_members_and_rows() -> None:
    contract = _load_json(PROOF_FIXTURE_DIR / "vscode" / "vscode-safestorage.contract.json")

    assert contract["stateDatabase"]["members"] == [
        "state.vscdb",
        "state.vscdb-wal",
        "state.vscdb-shm",
    ]
    assert contract["itemTable"]["access"] == "proof-recorded-rows-only"
    assert contract["characterizationStatus"] == "unproven"


def test_proof_record_schema_is_valid_and_accepts_a_sanitized_complete_record() -> None:
    schema = _load_json(PROOF_SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)

    Draft202012Validator(schema).validate(_valid_proof_record())


@pytest.mark.parametrize(
    "path",
    [
        ("identity",),
        ("revisions", "adapter"),
        ("fixture", "sha256"),
        ("validUntil",),
        ("results", "secretScan"),
        ("sanitization", "containsRawClientState"),
    ],
)
def test_proof_record_schema_rejects_missing_required_evidence(path: tuple[str, ...]) -> None:
    schema = _load_json(PROOF_SCHEMA_PATH)
    proof = _valid_proof_record()
    parent: dict[str, Any] = proof
    for key in path[:-1]:
        parent = parent[key]
    del parent[path[-1]]

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(proof)


@pytest.mark.parametrize("forbidden_key", sorted(FORBIDDEN_KEYS))
def test_proof_record_schema_rejects_secret_bearing_or_raw_state_fields(forbidden_key: str) -> None:
    schema = _load_json(PROOF_SCHEMA_PATH)
    proof = deepcopy(_valid_proof_record())
    proof[forbidden_key] = "forbidden"

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(proof)
