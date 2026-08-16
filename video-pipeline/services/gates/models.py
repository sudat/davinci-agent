from __future__ import annotations

from typing import Annotated, Literal, assert_never

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, SchemaVersion, Sha256, StrictModel
from services.gates.phase0a import PHASE_0A_CAPABILITIES, PHASE_0A_CRITERIA
from services.gates.phase0b import PHASE_0B_CRITERIA

type InputValue = (
    bool
    | int
    | float
    | str
    | list[InputValue]
    | tuple[InputValue, ...]
    | dict[str, InputValue]
    | None
)


def _as_immutable_sequence[Value](
    value: list[Value] | tuple[Value, ...],
) -> tuple[Value, ...]:
    return tuple(value)


type ImmutableSequence[Value] = Annotated[
    tuple[Value, ...],
    BeforeValidator(_as_immutable_sequence),
]


def _reject_floats(value: InputValue) -> InputValue:
    match value:
        case float():
            raise PydanticCustomError(
                "float_forbidden",
                "floats and NaN are forbidden in canonical Gate data",
            )
        case list():
            return [_reject_floats(item) for item in value]
        case tuple():
            return tuple(_reject_floats(item) for item in value)
        case dict():
            return {key: _reject_floats(item) for key, item in value.items()}
        case None | bool() | int() | str():
            return value
        case unreachable:
            assert_never(unreachable)


class GateModel(StrictModel):
    @model_validator(mode="before")
    @classmethod
    def reject_float_values(cls, value: InputValue) -> InputValue:
        return _reject_floats(value)


class GateResultBinding(GateModel):
    kind: Literal["gate_result"]
    gate_id: Identifier
    result_sha256: Sha256


class OperatorCheckpointBinding(GateModel):
    kind: Literal["operator_checkpoint"]
    purpose: Identifier
    episode_id: Identifier
    target_plan_sha256: Sha256
    target_timeline_ir_sha256: Sha256
    target_preview_sha256: Sha256
    operation_record_sha256: Sha256
    checkpoint_sha256: Sha256


PrerequisiteBinding = Annotated[
    GateResultBinding | OperatorCheckpointBinding,
    Field(discriminator="kind"),
]


class GatePolicy(GateModel):
    schema_version: SchemaVersion
    record_type: Literal["gate_policy"] = "gate_policy"
    purpose: Literal["gate_policy_freeze"] = "gate_policy_freeze"
    gate_id: Identifier
    gate_version: Identifier
    parent_gate_result_hashes: ImmutableSequence[Sha256]
    criteria: ImmutableSequence[Identifier]
    toolchain_lock_sha256: Sha256 | None
    fixture_manifest_sha256: Sha256 | None
    golden_sha256: Sha256 | None
    prerequisite_bindings: ImmutableSequence[PrerequisiteBinding]
    capability_allowlist: ImmutableSequence[Identifier]

    @model_validator(mode="after")
    def validate_phase_policy(self) -> GatePolicy:
        if self.gate_id == "phase-0a":
            return self._validate_phase_0a()
        if self.gate_id == "phase-0b":
            return self._validate_phase_0b()
        return self

    def _validate_phase_0a(self) -> GatePolicy:
        if self.criteria != PHASE_0A_CRITERIA:
            raise PydanticCustomError(
                "phase_0a_criteria",
                "Phase 0A criteria must match the canonical exit and Stop criteria",
            )
        if self.capability_allowlist != PHASE_0A_CAPABILITIES:
            raise PydanticCustomError(
                "phase_0a_capabilities",
                "Phase 0A capabilities must match the canonical allowlist",
            )
        if self.parent_gate_result_hashes or self.prerequisite_bindings:
            raise PydanticCustomError(
                "phase_0a_prerequisites",
                "Phase 0A requires empty parent and prerequisite binding lists",
            )
        return self

    def _validate_phase_0b(self) -> GatePolicy:
        if self.criteria != PHASE_0B_CRITERIA:
            raise PydanticCustomError(
                "phase_0b_criteria",
                "Phase 0B criteria must match the canonical conform exit criteria",
            )
        if self.capability_allowlist:
            raise PydanticCustomError(
                "phase_0b_capabilities",
                "Phase 0B freezes no capability allowlist; capabilities stay bound to 0A",
            )
        if self.prerequisite_bindings:
            raise PydanticCustomError(
                "phase_0b_prerequisites",
                "Phase 0B prerequisites are parent gate result hashes only",
            )
        if len(self.parent_gate_result_hashes) != 1:
            raise PydanticCustomError(
                "phase_0b_parent",
                "Phase 0B requires exactly one parent Phase 0A gate result hash",
            )
        return self


class EvidenceBundleRef(GateModel):
    bundle_sha256: Sha256
    raw_evidence_sha256s: ImmutableSequence[Sha256] = Field(min_length=1)


class CriterionResult(GateModel):
    criterion_id: Identifier
    passed: bool
    raw_evidence_sha256s: ImmutableSequence[Sha256] = Field(min_length=1)


class GateResult(GateModel):
    schema_version: SchemaVersion
    record_type: Literal["gate_result"] = "gate_result"
    gate_id: Identifier
    gate_version: Identifier
    policy_sha256: Sha256
    passed: bool
    evidence_bundles: ImmutableSequence[EvidenceBundleRef] = Field(min_length=1)
    criteria_results: ImmutableSequence[CriterionResult] = Field(min_length=1)

    @model_validator(mode="after")
    def require_recomputed_pass_state(self) -> GateResult:
        criterion_ids = tuple(result.criterion_id for result in self.criteria_results)
        if len(set(criterion_ids)) != len(criterion_ids):
            raise PydanticCustomError(
                "duplicate_criterion_result",
                "criterion results must contain unique criterion IDs",
            )
        recomputed = all(result.passed for result in self.criteria_results)
        if self.passed != recomputed:
            raise PydanticCustomError(
                "gate_pass_mismatch",
                "passed must equal the recomputed criteria result state",
            )
        return self
