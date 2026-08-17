from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, PositiveInteger, Sha256, StrictModel

type Sequence[Value] = Annotated[tuple[Value, ...], BeforeValidator(tuple)]

CONTROL_PLANE_FIXTURE_IDS: tuple[str, ...] = (
    "cp-atomic-publish",
    "cp-crash-before-rename",
    "cp-orphan-reconcile",
    "cp-stale-cas",
    "cp-lease-expiry",
    "cp-path-symlink-denial",
)

ScenarioKind = Literal[
    "atomic-publish",
    "crash-before-rename",
    "orphan-reconcile",
    "stale-cas",
    "lease-expiry",
    "path-symlink-denial",
]

OperationName = Literal[
    "publish",
    "publish-interrupted",
    "reopen",
    "reconcile",
    "plant-orphan-temp",
    "plant-symlink",
    "tamper-object",
    "acquire-lease",
    "expired-commit",
    "new-holder-acquire",
    "new-holder-commit",
]

FaultKind = Literal[
    "none",
    "kill-before-rename",
    "kill-after-rename-before-meta",
    "orphan-temp",
    "tamper-object",
    "lease-expiry",
    "symlink-path-component",
]

FaultPoint = Literal["none", "write-temp", "rename-object", "acquire-lease", "publish"]

_FAULT_POINT_BY_KIND: dict[str, str] = {
    "none": "none",
    "kill-before-rename": "write-temp",
    "kill-after-rename-before-meta": "rename-object",
    "orphan-temp": "none",
    "tamper-object": "publish",
    "lease-expiry": "acquire-lease",
    "symlink-path-component": "none",
}


class PayloadSpec(StrictModel):
    kind: Literal["utf8-text"]
    text: str = Field(min_length=1)
    sha256: Sha256

    @model_validator(mode="after")
    def require_pre_registered_hash(self) -> PayloadSpec:
        if hashlib.sha256(self.text.encode()).hexdigest() != self.sha256:
            raise PydanticCustomError(
                "payload_hash",
                "payload sha256 must be pre-registered exactly",
            )
        return self

    def payload_bytes(self) -> bytes:
        return self.text.encode()


class InjectedFault(StrictModel):
    kind: FaultKind
    after: FaultPoint

    @model_validator(mode="after")
    def require_declared_fault_point(self) -> InjectedFault:
        if _FAULT_POINT_BY_KIND[self.kind] != self.after:
            raise PydanticCustomError(
                "fault_point",
                "fault kind {kind} must be injected after {point}",
                {"kind": self.kind, "point": _FAULT_POINT_BY_KIND[self.kind]},
            )
        return self


class LeaseSpec(StrictModel):
    holder: Identifier
    challenger: Identifier
    ttl_seconds: PositiveInteger
    elapsed_past_expiry_seconds: PositiveInteger


class ControlPlaneScenario(StrictModel):
    kind: ScenarioKind
    artifact_id: Identifier
    payload: PayloadSpec
    lease: LeaseSpec | None
    operations: Sequence[OperationName] = Field(min_length=1)
    fault: InjectedFault

    @model_validator(mode="after")
    def require_lease_only_for_lease_scenario(self) -> ControlPlaneScenario:
        if (self.kind == "lease-expiry") != (self.lease is not None):
            raise PydanticCustomError(
                "lease_binding",
                "lease spec is required exactly for the lease-expiry scenario",
            )
        return self


class AfterFaultState(StrictModel):
    object_present: Literal[False]
    temp_present: Literal[True]


class FinalState(StrictModel):
    object_present: Literal[True]
    meta_present: Literal[True]


class AtomicPublishExpectation(StrictModel):
    kind: Literal["atomic-publish"]
    object_present: Literal[True]
    meta_present: Literal[True]
    temp_present: Literal[False]
    reopen_outcome: Literal["content-equal"]


class CrashRecoveryExpectation(StrictModel):
    kind: Literal["crash-before-rename"]
    after_fault: AfterFaultState
    reconcile_action: Literal["discard-crashed-temp"]
    republish_outcome: Literal["idempotent-success"]
    final: FinalState
    reopen_outcome: Literal["content-equal"]


class OrphanReconcileExpectation(StrictModel):
    kind: Literal["orphan-reconcile"]
    reconcile_action: Literal["discard-orphan-temp"]
    object_present: Literal[False]
    temp_present_after: Literal[False]
    reconcile_log_entries: Literal[1]


class StaleCasExpectation(StrictModel):
    kind: Literal["stale-cas"]
    reopen_outcome: Literal["hash-mismatch-rejected"]
    refusal_code: Literal["content-hash-mismatch"]


class LeaseExpiryExpectation(StrictModel):
    kind: Literal["lease-expiry"]
    expired_holder_commit: Literal["refused"]
    refusal_code: Literal["lease-expired"]
    new_holder_acquire: Literal["granted"]
    new_holder_commit: Literal["accepted"]


class PathSymlinkDenialExpectation(StrictModel):
    kind: Literal["path-symlink-denial"]
    publication: Literal["refused"]
    refusal_code: Literal["symlinked-path-component"]
    object_present: Literal[False]


type ControlPlaneExpectation = Annotated[
    AtomicPublishExpectation
    | CrashRecoveryExpectation
    | OrphanReconcileExpectation
    | StaleCasExpectation
    | LeaseExpiryExpectation
    | PathSymlinkDenialExpectation,
    Field(discriminator="kind"),
]


class ControlPlaneFixtureManifest(StrictModel):
    schema_version: Literal["control-plane-fixture-manifest-v1"]
    fixture_id: Literal[
        "cp-atomic-publish",
        "cp-crash-before-rename",
        "cp-orphan-reconcile",
        "cp-stale-cas",
        "cp-lease-expiry",
        "cp-path-symlink-denial",
    ]
    phase: Literal["control-plane"]
    scenario: ControlPlaneScenario
    expected: ControlPlaneExpectation
    expectation_basis: Literal["pre-registered-deterministic-store-reasoning"]

    def canonical_bytes(self) -> bytes:
        payload = self.model_dump(mode="json")
        return (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
            + b"\n"
        )

    @model_validator(mode="after")
    def require_kind_agreement(self) -> ControlPlaneFixtureManifest:
        if self.fixture_id != f"cp-{self.scenario.kind}":
            raise PydanticCustomError(
                "fixture_kind",
                "fixture id must be cp-<scenario kind>",
            )
        if self.expected.kind != self.scenario.kind:
            raise PydanticCustomError(
                "expectation_kind",
                "expected outcome kind must match the scenario kind",
            )
        return self

    @model_validator(mode="after")
    def reject_observed_result_fields(self) -> ControlPlaneFixtureManifest:
        stack: list[object] = [self.model_dump(mode="json", exclude_none=True)]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if "observed" in key:
                        raise PydanticCustomError(
                            "observed_result_field",
                            "manifests pre-register deterministic expectations; observed "
                            "outputs are not manifest fields",
                        )
                    stack.append(value)
            elif isinstance(node, list | tuple):
                stack.extend(node)
        return self
