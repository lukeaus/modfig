"""ModFig Registry 0.1 conformance fixtures.

Every valid fixture must be accepted by `modfig.registry.load_registry_text`; every
invalid fixture must be rejected with `RegistryValidationError`. The fixture
suite is the normative conformance surface for CLI 0.1.0 (FR-13, AC-13.1/13.2).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker

from modfig.registry import RegistryValidationError, load_registry_text

SPEC_DIR = Path(__file__).resolve().parent.parent / "spec"
VALID_DIR = SPEC_DIR / "fixtures" / "valid"
INVALID_DIR = SPEC_DIR / "fixtures" / "invalid"
SCHEMA_PATH = SPEC_DIR / "modfig-registry-0.1.schema.json"
MARKDOWN_SPEC_PATH = SPEC_DIR / "modfig-registry-0.1.md"

VALID_FIXTURES = sorted(VALID_DIR.rglob("*.yaml"))
INVALID_FIXTURES = sorted(INVALID_DIR.rglob("*.yaml"))
SEMANTIC_ONLY_INVALID_FIXTURES = {
    "base-url-fragment.yaml",
    "chatgpt-anthropic-transport.yaml",
    "chatgpt-default-whitespace-catalog-id.yaml",
    "chatgpt-generic-transport.yaml",
    "chatgpt-missing-transport.yaml",
    "chatgpt-two-defaults.yaml",
    "chatgpt-default-non-emitting.yaml",
    "chatgpt-unsafe-profile-key.yaml",
    "chatgpt-zero-defaults.yaml",
    "duplicate-chatgpt-catalog-id.yaml",
    "duplicate-chatgpt-provider-id.yaml",
    "duplicate-factory-id.yaml",
    "duplicate-vscode-id.yaml",
    "duplicate-yaml-key.yaml",
    "insecure-http-endpoint.yaml",
}
STRUCTURAL_INVALID_FIXTURES = [
    path for path in INVALID_FIXTURES if path.name not in SEMANTIC_ONLY_INVALID_FIXTURES
]


def schema_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_normative_specification_is_published() -> None:
    assert MARKDOWN_SPEC_PATH.is_file(), "spec/modfig-registry-0.1.md must exist"
    text = MARKDOWN_SPEC_PATH.read_text(encoding="utf-8")
    assert "# ModFig Registry Specification 0.1" in text
    assert "| Field | Type | Required |" in text


def test_v01_specification_documents_client_adapter_boundary() -> None:
    text = MARKDOWN_SPEC_PATH.read_text(encoding="utf-8")

    for fragment in (
        "## Local adapter routing",
        "adapters.yaml",
        "`modfig.adapters.v1`",
        "trusted in-process code, not a sandbox",
        "`validate --adapters`",
        "desired-or-owned",
        "## Examples",
        "`clientConfig.factory.extensions.oh-my-droid`",
        "`clientConfig.cursor.core`",
    ):
        assert fragment in text

    """AC-1.5/NFR-5: a machine-readable, well-formed JSON Schema is published."""
    assert SCHEMA_PATH.is_file(), "spec/modfig-registry-0.1.schema.json must exist"
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema.get("$schema", "").startswith("https://json-schema.org/")
    assert schema.get("title") == "ModFig Registry Specification 0.1"


def test_fixture_suite_is_populated() -> None:
    """AC-13.1/13.2: valid and invalid fixture suites must exist and be non-empty."""
    assert VALID_FIXTURES, "no valid fixtures under spec/fixtures/valid"
    assert INVALID_FIXTURES, "no invalid fixtures under spec/fixtures/invalid"


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.name)
def test_valid_fixture_is_accepted(path: Path) -> None:
    content = path.read_text(encoding="utf-8")
    registry = load_registry_text(content)
    schema_validator().validate(yaml.safe_load(content))
    assert registry.spec_version == "0.1"
    assert registry.providers, "valid registry must declare at least one provider"


@pytest.mark.parametrize("path", INVALID_FIXTURES, ids=lambda p: p.name)
def test_invalid_fixture_is_rejected(path: Path) -> None:
    with pytest.raises(RegistryValidationError):
        load_registry_text(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", STRUCTURAL_INVALID_FIXTURES, ids=lambda p: p.name)
def test_structural_invalid_fixture_is_rejected_by_schema(path: Path) -> None:
    errors = list(schema_validator().iter_errors(yaml.safe_load(path.read_text(encoding="utf-8"))))
    assert errors, f"{path.name} must be rejected by the published schema"
