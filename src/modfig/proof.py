from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

from modfig.resources import read_resource_bytes

_LOGICAL_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_FORBIDDEN_KEYS = {
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
_RESULT_NAMES = {
    "authenticatedRequest",
    "restartReload",
    "foreignStatePreservation",
    "recovery",
    "secretScan",
}


@dataclass(frozen=True)
class ReleaseRevisions:
    adapter: str
    spec: str
    artifact: str


def _load_json_resource(path: str) -> dict[str, Any]:
    value = json.loads(read_resource_bytes(path))
    if not isinstance(value, dict):
        raise ValueError(f"resource must contain a JSON object: {path}")
    return cast(dict[str, Any], value)


def load_capability_matrix() -> dict[str, Any]:
    return _load_json_resource("spec/capability-matrix.json")


def _rows(matrix: Mapping[str, Any]) -> list[dict[str, Any]]:
    if matrix.get("matrixVersion") != 1 or matrix.get("proofSchema") != "proof-record.schema.json":
        raise ValueError("invalid capability matrix header")
    rows = matrix.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("capability matrix rows must be a non-empty array")

    checked: list[dict[str, Any]] = []
    ids: set[str] = set()
    identities: set[tuple[Any, ...]] = set()
    for value in rows:
        if not isinstance(value, dict):
            raise ValueError("capability matrix row must be an object")
        row = cast(dict[str, Any], value)
        row_id = row.get("id")
        component = row.get("component")
        identity = (
            row.get("logicalClient"),
            json.dumps(component, sort_keys=True) if isinstance(component, dict) else None,
            row.get("adapterId"),
            row.get("featureKey"),
            row.get("os"),
            row.get("osBuild"),
            row.get("surface"),
            row.get("channel"),
            row.get("profileMode"),
            row.get("clientVersion"),
        )
        logical_client = row.get("logicalClient")
        valid_component = component == {"kind": "core"} or (
            isinstance(component, dict)
            and set(component) == {"kind", "name"}
            and component.get("kind") == "extension"
            and isinstance(component.get("name"), str)
            and component.get("name") != "core"
            and _LOGICAL_ID_RE.fullmatch(component["name"])
        )
        if (
            not isinstance(logical_client, str)
            or not _LOGICAL_ID_RE.fullmatch(logical_client)
            or not valid_component
            or not isinstance(row.get("adapterId"), str)
            or not row["adapterId"]
            or not isinstance(row.get("featureKey"), str)
            or not row["featureKey"]
        ):
            raise ValueError(f"malformed capability row identity: {row_id}")
        if not isinstance(row_id, str) or not row_id or row_id in ids or identity in identities:
            raise ValueError("capability matrix rows must have unique identities")
        ids.add(row_id)
        identities.add(identity)

        status = row.get("supportStatus")
        if status == "unsupported":
            if (
                not isinstance(row.get("unsupportedReason"), str)
                or not row["unsupportedReason"]
                or row.get("freshnessDays") is not None
                or row.get("requiredEvidence") != []
            ):
                raise ValueError(f"malformed unsupported capability row: {row_id}")
        elif status == "supported":
            freshness = row.get("freshnessDays")
            evidence = row.get("requiredEvidence")
            if (
                not isinstance(freshness, int)
                or isinstance(freshness, bool)
                or freshness <= 0
                or not isinstance(evidence, list)
                or set(evidence) != _RESULT_NAMES
                or not isinstance(row.get("paths"), list)
                or not row["paths"]
                or not isinstance(row.get("contractFixture"), dict)
            ):
                raise ValueError(f"malformed supported capability row: {row_id}")
        else:
            raise ValueError(f"unknown capability support status: {row_id}")
        contract_fixture = row.get("contractFixture")
        if contract_fixture is not None:
            if (
                not isinstance(contract_fixture, dict)
                or not isinstance(contract_fixture.get("path"), str)
                or not contract_fixture["path"]
                or not isinstance(contract_fixture.get("sha256"), str)
            ):
                raise ValueError(f"malformed capability row: {row_id}")
            actual_hash = (
                "sha256:"
                + hashlib.sha256(read_resource_bytes(contract_fixture["path"])).hexdigest()
            )
            if actual_hash != contract_fixture["sha256"]:
                raise ValueError(f"capability row fixture hash mismatch: {row_id}")
        checked.append(row)
    return checked


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"invalid proof timestamp: {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid proof timestamp: {field}") from error
    if parsed.utcoffset() is None:
        raise ValueError(f"invalid proof timestamp: {field}")
    return parsed


def _reject_secrets(value: object, sentinels: tuple[str, ...]) -> None:
    if isinstance(value, Mapping):
        if _FORBIDDEN_KEYS.intersection(value):
            raise ValueError("proof contains forbidden raw state")
        for nested in value.values():
            _reject_secrets(nested, sentinels)
    elif isinstance(value, list):
        for nested in value:
            _reject_secrets(nested, sentinels)
    elif isinstance(value, str) and any(sentinel and sentinel in value for sentinel in sentinels):
        raise ValueError("proof contains a secret sentinel")


def _validate_record(
    record: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    now: datetime,
    revisions: ReleaseRevisions,
    validator: Draft202012Validator,
    sentinels: tuple[str, ...],
) -> None:
    _reject_secrets(record, sentinels)
    errors = sorted(validator.iter_errors(record), key=lambda error: list(error.absolute_path))
    if errors:
        raise ValueError("proof record does not match packaged schema")

    identity = record["identity"]
    os_identity = identity["os"]
    client = identity["client"]
    expected = (
        row["logicalClient"],
        row["component"],
        row["adapterId"],
        row["featureKey"],
        row["os"],
        row["osBuild"],
        row["surface"],
        row["channel"],
        row["profileMode"],
        row["clientVersion"],
    )
    actual = (
        identity["logicalClient"],
        identity["component"],
        identity["adapterId"],
        identity["featureKey"],
        os_identity["family"],
        os_identity["build"],
        identity["surface"],
        identity["channel"],
        identity["profileMode"],
        client["version"],
    )
    if actual != expected:
        raise ValueError("proof identity does not match capability row")
    if [path["path"] for path in record["paths"]] != row["paths"]:
        raise ValueError("proof paths do not match capability row")
    if (
        record["credentialMechanism"] != row["credentialMechanism"]
        or record["processCheck"]["detectorId"] != row["processDetector"]
        or record["fixture"] != row["contractFixture"]
    ):
        raise ValueError("proof contract does not match capability row")
    if record["revisions"] != {
        "adapter": revisions.adapter,
        "spec": revisions.spec,
        "artifact": revisions.artifact,
    }:
        raise ValueError("proof revisions do not match release")

    started = _parse_time(record["startedAt"], "startedAt")
    completed = _parse_time(record["completedAt"], "completedAt")
    valid_until = _parse_time(record["validUntil"], "validUntil")
    if not started <= completed <= now <= valid_until:
        raise ValueError("proof timestamps are not current")
    if valid_until > completed + timedelta(days=row["freshnessDays"]):
        raise ValueError("proof validity exceeds capability freshness")
    if any(result["status"] != "pass" for result in record["results"].values()):
        raise ValueError("proof contains a failed result")
    if record["processCheck"]["quiescent"] is not True:
        raise ValueError("processCheck is not quiescent")


def validate_release_evidence(
    proof_records: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    revisions: ReleaseRevisions,
    secret_sentinels: Iterable[str] = (),
) -> None:
    if now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    rows = _rows(load_capability_matrix())
    rows_by_id = {row["id"]: row for row in rows}
    records_by_id: dict[str, Mapping[str, Any]] = {}
    for record in proof_records:
        row_id = record.get("rowId")
        if not isinstance(row_id, str) or row_id not in rows_by_id:
            raise ValueError("proof references an unknown capability row")
        if row_id in records_by_id:
            raise ValueError("duplicate proof record")
        if rows_by_id[row_id]["supportStatus"] == "unsupported":
            raise ValueError("proof supplied for unsupported row")
        records_by_id[row_id] = record

    schema = _load_json_resource("spec/proof-record.schema.json")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    sentinels = tuple(secret_sentinels)
    for row in rows:
        if row["supportStatus"] == "supported":
            current_record = records_by_id.get(row["id"])
            if current_record is None:
                raise ValueError("supported capability row is missing proof")
            _validate_record(
                current_record,
                row,
                now=now,
                revisions=revisions,
                validator=validator,
                sentinels=sentinels,
            )
