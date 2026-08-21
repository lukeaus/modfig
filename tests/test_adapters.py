from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path, PurePosixPath

import pytest

from modfig.adapter_routes import AdapterRoute, PathGrant
from modfig.adapters import (
    AbsentDestination,
    AdapterContext,
    AdapterMetadata,
    AdapterPlanContext,
    AdapterPlanError,
    AdapterRouteError,
    AdapterV1,
    ArtifactIdentity,
    ArtifactPlan,
    PlannedArtifact,
    PreflightDeclaration,
    ProspectiveWrite,
    ResolvedModel,
    RuntimeProof,
    SnapshotRequest,
    discover_adapter_entry_points,
    load_enabled_adapter,
    validate_artifact_relative_path,
    validate_feature_key,
    validate_json_safe,
    validate_plan_against_declarations,
)
from modfig.components import ExtensionComponent
from modfig.registry import ModelReference


@dataclass
class _Distribution:
    name: str
    entry_points: list[object] | None = None

    def __post_init__(self) -> None:
        if self.entry_points is None:
            self.entry_points = []


class _EntryPoint:
    def __init__(self, name: str, distribution: str, adapter: object) -> None:
        self.name = name
        self.group = "modfig.adapters.v1"
        self.value = "example_adapter:adapter"
        self.dist = _Distribution(distribution, [self])
        self._adapter = adapter
        self.loaded = 0

    def load(self) -> object:
        self.loaded += 1
        return self._adapter


class _Adapter:
    def __init__(self, metadata: AdapterMetadata) -> None:
        self.metadata = metadata
        self.described = 0

    def describe(self) -> AdapterMetadata:
        self.described += 1
        return self.metadata

    def validate(self, config: object, context: object) -> None:
        del config, context

    def preflight(self, context: object) -> object:
        del context
        raise AssertionError("not called")

    def plan(self, *args: object) -> object:
        del args
        raise AssertionError("not called")

    def recheck(self, proof: object) -> None:
        del proof
        raise AssertionError("not called")

    def verify(self, *args: object) -> None:
        del args
        raise AssertionError("not called")


def _route(*, enabled: bool = True, distribution: str = "example-cursor") -> AdapterRoute:
    return AdapterRoute(
        logical_client="cursor",
        component="core",
        adapter_id="io.example.cursor",
        distribution=distribution,
        enabled=enabled,
        read_grants=(PathGrant("read", "file", Path.home() / "read", None),),
        write_grants=(PathGrant("write", "file", Path.home() / "write", None),),
    )


def test_discovery_enumerates_without_importing(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _Adapter(AdapterMetadata("io.example.cursor", "cursor", "core"))
    entry_point = _EntryPoint("io.example.cursor", "example-cursor", adapter)
    monkeypatch.setattr(
        "modfig.adapters.importlib.metadata.entry_points", lambda **kwargs: [entry_point]
    )

    assert discover_adapter_entry_points()["io.example.cursor"] is entry_point
    assert entry_point.loaded == 0


def test_discovery_rejects_duplicate_authoritative_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _Adapter(AdapterMetadata("io.example.cursor", "cursor", "core"))
    first = _EntryPoint("io.example.cursor", "first", adapter)
    second = _EntryPoint("io.example.cursor", "second", adapter)
    monkeypatch.setattr(
        "modfig.adapters.importlib.metadata.entry_points", lambda **kwargs: [first, second]
    )

    with pytest.raises(AdapterRouteError, match="duplicate.*io.example.cursor"):
        discover_adapter_entry_points()
    assert first.loaded == second.loaded == 0


def test_load_enabled_adapter_verifies_provenance_before_one_import() -> None:
    route = _route()
    adapter = _Adapter(AdapterMetadata(route.adapter_id, route.logical_client, route.component))
    entry_point = _EntryPoint(route.adapter_id, route.distribution, adapter)

    loaded = load_enabled_adapter(route, entry_points={route.adapter_id: entry_point})

    assert loaded is adapter
    assert entry_point.loaded == 1
    assert adapter.described == 1


def test_load_enabled_adapter_verifies_distribution_ownership_before_import() -> None:
    route = _route()
    adapter = _Adapter(AdapterMetadata(route.adapter_id, route.logical_client, route.component))
    entry_point = _EntryPoint(route.adapter_id, route.distribution, adapter)
    other = _EntryPoint(route.adapter_id, route.distribution, adapter)
    other.value = "other_adapter:adapter"
    assert entry_point.dist is not None
    entry_point.dist.entry_points = [other]

    with pytest.raises(AdapterRouteError, match="distribution.*ownership"):
        load_enabled_adapter(route, entry_points={route.adapter_id: entry_point})
    assert entry_point.loaded == 0


@pytest.mark.parametrize(
    "route, entry_distribution, metadata, message",
    [
        (
            _route(enabled=False),
            "example-cursor",
            AdapterMetadata("io.example.cursor", "cursor", "core"),
            "disabled",
        ),
        (
            _route(distribution="expected"),
            "other",
            AdapterMetadata("io.example.cursor", "cursor", "core"),
            "distribution",
        ),
        (
            _route(),
            "example-cursor",
            AdapterMetadata("io.example.cursor", "factory", "core"),
            "metadata",
        ),
    ],
)
def test_load_enabled_adapter_rejects_invalid_binding(
    route: AdapterRoute,
    entry_distribution: str,
    metadata: AdapterMetadata,
    message: str,
) -> None:
    adapter = _Adapter(metadata)
    entry_point = _EntryPoint(route.adapter_id, entry_distribution, adapter)

    with pytest.raises(AdapterRouteError, match=message):
        load_enabled_adapter(route, entry_points={route.adapter_id: entry_point})
    if message in {"disabled", "distribution"}:
        assert entry_point.loaded == 0


# --- Task 5: adapter-plan contract closure ---


def _identity(path: str = "settings.json", grant: str = "write") -> ArtifactIdentity:
    return ArtifactIdentity(grant, PurePosixPath(path))


def test_absent_destination_is_frozen_singleton_value() -> None:
    sentinel = AbsentDestination()
    other = AbsentDestination()
    assert sentinel == other
    assert hash(sentinel) == hash(other)
    assert sentinel is not None
    assert sentinel != b""


def test_planned_artifact_accepts_absent_destination() -> None:
    planned = PlannedArtifact(_identity(), AbsentDestination(), "features.core.models", {})
    assert isinstance(planned.planned, AbsentDestination)


def test_planned_artifact_accepts_bytes() -> None:
    planned = PlannedArtifact(_identity(), b"{}", "features.core.models", {})
    assert planned.planned == b"{}"


def test_planned_artifact_rejects_none_planned() -> None:
    with pytest.raises(AdapterPlanError, match="planned"):
        PlannedArtifact(_identity(), None, "features.core.models", {})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "path",
    [
        "/abs.json",
        "../outside.json",
        "a/../b.json",
        "a/../../b.json",
        "a\\b.json",
    ],
)
def test_validate_artifact_relative_path_rejects_unsafe(path: str) -> None:
    with pytest.raises(AdapterPlanError, match="relative path"):
        validate_artifact_relative_path(PurePosixPath(path))


def test_validate_artifact_relative_path_accepts_clean_relative() -> None:
    validate_artifact_relative_path(PurePosixPath("dir/settings.json"))


def test_artifact_identity_rejects_escaping_path() -> None:
    with pytest.raises(AdapterPlanError, match="relative path"):
        ArtifactIdentity("write", PurePosixPath("../outside.json"))


def test_validate_feature_key_rejects_empty() -> None:
    with pytest.raises(AdapterPlanError, match="feature key"):
        validate_feature_key("")


def test_planned_artifact_rejects_empty_feature_key() -> None:
    with pytest.raises(AdapterPlanError, match="feature key"):
        PlannedArtifact(_identity(), b"{}", "", {})


def test_validate_json_safe_rejects_nonfinite_floats() -> None:
    with pytest.raises(AdapterPlanError, match="JSON-safe"):
        validate_json_safe({"x": float("nan")}, "reconciliation")


def test_validate_json_safe_rejects_inf() -> None:
    with pytest.raises(AdapterPlanError, match="JSON-safe"):
        validate_json_safe({"x": float("inf")}, "ownership")


def test_planned_artifact_rejects_non_json_safe_reconciliation() -> None:
    with pytest.raises(AdapterPlanError, match="reconciliation"):
        PlannedArtifact(_identity(), b"{}", "features.core.models", {"x": float("nan")})


def test_artifact_plan_rejects_non_json_safe_ownership() -> None:
    artifact = PlannedArtifact(_identity(), b"{}", "features.core.models", {})
    with pytest.raises(AdapterPlanError, match="ownership"):
        ArtifactPlan((artifact,), {"x": float("inf")})


def test_artifact_plan_rejects_duplicate_identities() -> None:
    artifact = PlannedArtifact(_identity(), b"{}", "features.core.models", {})
    with pytest.raises(AdapterPlanError, match="duplicate"):
        ArtifactPlan((artifact, artifact), {})


def test_preflight_declaration_rejects_duplicate_prospective_writes() -> None:
    write = ProspectiveWrite(_identity())
    with pytest.raises(AdapterPlanError, match="duplicate"):
        PreflightDeclaration({}, (), (write, write))


def test_preflight_declaration_rejects_non_json_safe_proof_requirements() -> None:
    with pytest.raises(AdapterPlanError, match="proof requirements"):
        PreflightDeclaration({"x": float("nan")}, (), ())


def test_validate_plan_against_declarations_rejects_undeclared_artifact() -> None:
    declared = _identity("declared.json")
    undeclared = _identity("undeclared.json")
    plan = ArtifactPlan(
        (PlannedArtifact(undeclared, b"{}", "features.core.models", {}),),
        {},
    )
    declaration = PreflightDeclaration({}, (), (ProspectiveWrite(declared),))
    with pytest.raises(AdapterPlanError, match="prospective write"):
        validate_plan_against_declarations(
            plan, declaration, AdapterPlanContext("cursor", "core", {})
        )


def test_validate_plan_against_declarations_accepts_declared_artifact() -> None:
    identity = _identity()
    plan = ArtifactPlan(
        (PlannedArtifact(identity, b"{}", "features.core.models", {}),),
        {},
    )
    declaration = PreflightDeclaration({}, (), (ProspectiveWrite(identity),))
    validate_plan_against_declarations(plan, declaration, AdapterPlanContext("cursor", "core", {}))


def test_validate_plan_rejects_feature_key_from_another_component() -> None:
    identity = _identity()
    plan = ArtifactPlan(
        (PlannedArtifact(identity, b"{}", "features.core.models", {}),),
        {},
    )
    declaration = PreflightDeclaration({}, (), (ProspectiveWrite(identity),))
    context = AdapterPlanContext("factory", ExtensionComponent("oh-my-droid"), {})

    with pytest.raises(AdapterPlanError, match="component"):
        validate_plan_against_declarations(plan, declaration, context)


def test_snapshot_request_validates_artifact_identity() -> None:
    with pytest.raises(AdapterPlanError, match="relative path"):
        SnapshotRequest(ArtifactIdentity("read", PurePosixPath("../escape.json")))


def test_prospective_write_validates_artifact_identity() -> None:
    with pytest.raises(AdapterPlanError, match="relative path"):
        ProspectiveWrite(ArtifactIdentity("write", PurePosixPath("/abs.json")))


def test_adapter_plan_context_freezes_bounded_models_and_resolver() -> None:
    model = ResolvedModel(
        provider_key="router",
        base_url="https://router.example/v1",
        api_key_reference="env.ROUTER_KEY",
        model="primary",
        display_name="Primary",
        max_output_tokens=1024,
        effective_provider="openai",
        no_image_support=False,
        favourite=True,
        factory_id="custom:primary--router",
    )
    models = [model]
    context = AdapterPlanContext(
        "factory",
        "core",
        {},
        models,
        lambda reference: (
            model
            if (reference.provider_key, reference.model_name) == ("router", "primary")
            else (_ for _ in ()).throw(LookupError(reference))
        ),
    )

    models.clear()

    assert context.models == (model,)
    assert context.resolve_model(ModelReference("router", "primary")) == model
    assert not hasattr(model, "models")
    assert not hasattr(model, "extensions")


def test_adapter_plan_context_rejects_resolver_result_outside_snapshot() -> None:
    model = ResolvedModel(
        provider_key="router",
        base_url="https://router.example/v1",
        api_key_reference="env.ROUTER_KEY",
        model="primary",
        display_name="Primary",
        max_output_tokens=1024,
        effective_provider="openai",
        no_image_support=False,
        favourite=False,
        factory_id="custom:primary--router",
    )
    context = AdapterPlanContext("factory", "core", {}, (), lambda reference: model)

    with pytest.raises(AdapterPlanError, match="snapshot"):
        context.resolve_model(ModelReference("router", "primary"))


def test_adapter_plan_context_freezes_selected_config() -> None:
    config: dict[str, object] = {"k": "v"}
    context = AdapterPlanContext("cursor", "core", config)
    assert context.selected_config == {"k": "v"}
    config["k"] = "mutated"
    assert context.selected_config == {"k": "v"}


def test_adapter_plan_context_is_immutable() -> None:
    context = AdapterPlanContext("cursor", "core", {"k": "v"})
    with pytest.raises(FrozenInstanceError):
        context.logical_client = "other"  # type: ignore[misc]


def test_contract_payloads_are_recursively_immutable() -> None:
    config = {"nested": {"items": ["original"]}}
    proof_facts = {"nested": {"items": ["original"]}}
    reconciliation = {"nested": {"items": ["original"]}}
    ownership = {"nested": {"items": ["original"]}}
    context = AdapterPlanContext("cursor", "core", config)
    proof = RuntimeProof(proof_facts, "declaration")
    artifact = PlannedArtifact(_identity(), b"{}", "features.core.models", reconciliation)
    plan = ArtifactPlan((artifact,), ownership)

    config["nested"]["items"].append("mutated")
    proof_facts["nested"]["items"].append("mutated")
    reconciliation["nested"]["items"].append("mutated")
    ownership["nested"]["items"].append("mutated")

    assert context.selected_config["nested"]["items"] == ("original",)  # type: ignore[index]
    assert proof.facts["nested"]["items"] == ("original",)  # type: ignore[index]
    assert artifact.reconciliation["nested"]["items"] == ("original",)  # type: ignore[index]
    assert plan.ownership["nested"]["items"] == ("original",)  # type: ignore[index]


def test_runtime_proof_accepts_previously_frozen_facts() -> None:
    original = RuntimeProof({"nested": {"items": ["original"]}}, "declaration")

    copied = RuntimeProof(original.facts, "other-declaration")

    assert copied.facts["nested"]["items"] == ("original",)  # type: ignore[index]


def test_adapter_v1_runtime_protocol_accepts_the_documented_lifecycle() -> None:
    class _PlanAdapter:
        def describe(self) -> AdapterMetadata:
            return AdapterMetadata("io.example.cursor", "cursor", "core")

        def validate(self, config: object, context: object) -> None:
            del config, context

        def preflight(self, context: AdapterContext) -> PreflightDeclaration:
            del context
            return PreflightDeclaration({}, (), ())

        def plan(
            self,
            context: AdapterPlanContext,
            proof: object,
            snapshots: object,
            ownership: object,
        ) -> ArtifactPlan:
            del context, proof, snapshots, ownership
            raise AssertionError("not called")

        def recheck(self, proof: object) -> None:
            del proof

        def verify(self, context: object, proof: object, written: object) -> None:
            del context, proof, written

    assert isinstance(_PlanAdapter(), AdapterV1)
