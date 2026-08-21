from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

from modfig.adapters import (
    AbsentDestination,
    AdapterContext,
    AdapterMetadata,
    AdapterPlanContext,
    AdapterValidationContext,
    ArtifactIdentity,
    ArtifactPlan,
    ArtifactSnapshot,
    PlannedArtifact,
    PreflightDeclaration,
    ProspectiveWrite,
    RuntimeProof,
    SnapshotRequest,
)
from modfig.components import ExtensionComponent

_SOURCE = ArtifactIdentity("plugin-read", PurePosixPath("source.json"))
_DESTINATION = ArtifactIdentity("plugin-write", PurePosixPath("rendered.json"))


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


class FakeOhMyDroidAdapter:
    def describe(self) -> AdapterMetadata:
        return AdapterMetadata("io.example.helper", "factory", ExtensionComponent("helper"))

    def validate(self, config: Mapping[str, object], context: AdapterValidationContext) -> None:
        if context.logical_client != "factory" or context.component != ExtensionComponent("helper"):
            raise ValueError("fake adapter binding mismatch")
        if config and not isinstance(config, Mapping):
            raise ValueError("fake extension configuration must be a mapping")

    def preflight(self, context: AdapterContext) -> PreflightDeclaration:
        if context != AdapterContext("factory", ExtensionComponent("helper")):
            raise ValueError("fake adapter preflight binding mismatch")
        return PreflightDeclaration(
            {"proof": "host-supplied"},
            (SnapshotRequest(_SOURCE),),
            (ProspectiveWrite(_DESTINATION),),
        )

    def plan(
        self,
        context: AdapterPlanContext,
        proof: RuntimeProof,
        snapshots: Mapping[ArtifactIdentity, ArtifactSnapshot],
        ownership: Mapping[str, object],
    ) -> ArtifactPlan:
        del proof
        if context.logical_client != "factory" or context.component != ExtensionComponent("helper"):
            raise ValueError("fake adapter plan binding mismatch")
        if not context.selected_config and ownership:
            return ArtifactPlan(
                (PlannedArtifact(_DESTINATION, AbsentDestination(), "features.helper.render", {}),),
                {},
            )
        source = snapshots[_SOURCE]
        rendered = json.dumps(
            {
                "config": _json_value(context.selected_config),
                "sourceSha256": None
                if isinstance(source, AbsentDestination)
                else hashlib.sha256(source).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return ArtifactPlan(
            (
                PlannedArtifact(
                    _DESTINATION,
                    rendered,
                    "features.helper.render",
                    {"source": "plugin-read"},
                ),
            ),
            {"renderer": "fake-helper"},
        )

    def recheck(self, proof: RuntimeProof) -> None:
        del proof

    def verify(
        self,
        context: AdapterContext,
        proof: RuntimeProof,
        written: Sequence[ArtifactSnapshot],
    ) -> None:
        del proof
        if context != AdapterContext("factory", ExtensionComponent("helper")):
            raise ValueError("fake adapter verification binding mismatch")
        if len(written) != 1:
            raise ValueError("fake adapter expected one written artifact")


adapter = FakeOhMyDroidAdapter()
