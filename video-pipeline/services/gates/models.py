from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, SchemaVersion, Sha256, StrictModel
from services.gates.control_plane import PHASE_1_CONTROL_PLANE_CRITERIA
from services.gates.phase0a import PHASE_0A_CAPABILITIES, PHASE_0A_CRITERIA
from services.gates.phase0b import PHASE_0B_CRITERIA
from services.gates.phase0c import PHASE_0C_CRITERIA
from services.gates.phase1_technical import (
    PHASE_1_PARENT_COUNT,
    PHASE_1_TECHNICAL_CRITERIA,
)
from services.gates.phase2 import (
    PHASE_2_CAPABILITIES,
    PHASE_2_CRITERIA,
    PHASE_2_PARENT_COUNT,
)
from services.gates.phase3 import (
    PHASE_3_CRITERIA,
    PHASE_3_PARENT_COUNT,
)

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
        case object():
            # strict submodels forbid floats in their own fields, so pass through
            return value


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
        phase_validators: dict[str, Callable[[], GatePolicy]] = {
            "phase-0a": self._validate_phase_0a,
            "phase-0b": self._validate_phase_0b,
            "phase-0c": self._validate_phase_0c,
            "control-plane-baseline": self._validate_control_plane,
            "phase-1-technical": self._validate_phase_1_technical,
            "phase-2": self._validate_phase_2,
            "phase-3": self._validate_phase_3,
        }
        validator = phase_validators.get(self.gate_id)
        if validator is None:
            return self
        return validator()

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

    def _validate_phase_0c(self) -> GatePolicy:
        if self.criteria != PHASE_0C_CRITERIA:
            raise PydanticCustomError(
                "phase_0c_criteria",
                "Phase 0C criteria must match the canonical review/compiler exit criteria",
            )
        if self.capability_allowlist:
            raise PydanticCustomError(
                "phase_0c_capabilities",
                "Phase 0C freezes no capability allowlist; capabilities stay bound to 0A",
            )
        if self.prerequisite_bindings:
            raise PydanticCustomError(
                "phase_0c_prerequisites",
                "Phase 0C prerequisites are parent gate result hashes only",
            )
        if len(self.parent_gate_result_hashes) != 1:
            raise PydanticCustomError(
                "phase_0c_parent",
                "Phase 0C requires exactly one parent Phase 0B gate result hash",
            )
        return self

    def _validate_control_plane(self) -> GatePolicy:
        if self.criteria != PHASE_1_CONTROL_PLANE_CRITERIA:
            raise PydanticCustomError(
                "phase_1_control_plane_criteria",
                "Control Plane criteria must match the canonical baseline exit criteria",
            )
        if self.capability_allowlist:
            raise PydanticCustomError(
                "phase_1_control_plane_capabilities",
                "Control Plane baseline freezes no capability allowlist",
            )
        if self.prerequisite_bindings:
            raise PydanticCustomError(
                "phase_1_control_plane_prerequisites",
                "Control Plane baseline prerequisites are parent gate result hashes only",
            )
        if len(self.parent_gate_result_hashes) != 1:
            raise PydanticCustomError(
                "phase_1_control_plane_parent",
                "Control Plane baseline requires exactly one parent Phase 0C gate result hash",
            )
        if self.golden_sha256 is not None:
            raise PydanticCustomError(
                "phase_1_control_plane_golden",
                "Control Plane fixtures carry no media/model Golden derivation",
            )
        return self

    def _validate_phase_1_technical(self) -> GatePolicy:
        if self.criteria != PHASE_1_TECHNICAL_CRITERIA:
            raise PydanticCustomError(
                "phase_1_technical_criteria",
                "Phase-1 technical criteria must match the canonical fixture/rule criteria",
            )
        contract_out = any(
            "kpi" in criterion or "active-human-time" in criterion for criterion in self.criteria
        )
        if contract_out:
            raise PydanticCustomError(
                "phase_1_technical_kpi_contract_out",
                "Technical fixtures prove deterministic mechanics only; "
                "KPI criteria are contract-out",
            )
        if self.capability_allowlist:
            raise PydanticCustomError(
                "phase_1_technical_capabilities",
                "Phase-1 technical freezes no capability allowlist; capabilities stay bound to 0A",
            )
        if self.prerequisite_bindings:
            raise PydanticCustomError(
                "phase_1_technical_prerequisites",
                "Phase-1 technical prerequisites are parent gate result hashes only",
            )
        if len(self.parent_gate_result_hashes) != PHASE_1_PARENT_COUNT:
            raise PydanticCustomError(
                "phase_1_technical_parent",
                "Phase-1 technical requires exactly two parent gate result hashes "
                "(phase-0c and control-plane-baseline)",
            )
        if self.golden_sha256 is None:
            raise PydanticCustomError(
                "phase_1_technical_golden",
                "Phase-1 technical fixtures require the independent Golden index binding",
            )
        return self

    def _validate_phase_2(self) -> GatePolicy:
        if self.criteria != PHASE_2_CRITERIA:
            raise PydanticCustomError(
                "phase_2_criteria",
                "Phase-2 criteria must match the canonical finalization exit criteria",
            )
        if self.capability_allowlist != PHASE_2_CAPABILITIES:
            raise PydanticCustomError(
                "phase_2_capabilities",
                "Phase-2 freezes exactly the five live-verified package capabilities",
            )
        if len(self.parent_gate_result_hashes) != PHASE_2_PARENT_COUNT:
            raise PydanticCustomError(
                "phase_2_parent",
                "Phase-2 requires exactly three parent gate result hashes "
                "(phase-0a, phase-0b, phase-1-technical)",
            )
        if len(self.prerequisite_bindings) != 1:
            raise PydanticCustomError(
                "phase_2_prerequisites",
                "Phase-2 requires exactly one operator-checkpoint prerequisite binding",
            )
        binding = self.prerequisite_bindings[0]
        if not isinstance(binding, OperatorCheckpointBinding):
            raise PydanticCustomError(
                "phase_2_prerequisites",
                "the Phase-2 prerequisite must be the H1 operator checkpoint",
            )
        if binding.purpose != "EDITORIAL_APPROVED":
            raise PydanticCustomError(
                "phase_2_prerequisites",
                "the Phase-2 prerequisite purpose must be EDITORIAL_APPROVED",
            )
        if self.golden_sha256 is None:
            raise PydanticCustomError(
                "phase_2_golden",
                "Phase-2 fixtures require the independent Golden index binding",
            )
        if self.toolchain_lock_sha256 is None or self.fixture_manifest_sha256 is None:
            raise PydanticCustomError(
                "phase_2_bindings",
                "Phase-2 requires the toolchain lock and fixture manifest bindings",
            )
        return self

    def _validate_phase_3(self) -> GatePolicy:
        if self.criteria != PHASE_3_CRITERIA:
            raise PydanticCustomError(
                "phase_3_criteria",
                "Phase-3 criteria must match the canonical A/B profile-swap exit criteria",
            )
        if self.capability_allowlist:
            raise PydanticCustomError(
                "phase_3_capabilities",
                "Phase-3 freezes no new capability allowlist; the gate re-runs the "
                "frozen phase-2 capabilities through its parent binding",
            )
        if self.prerequisite_bindings:
            raise PydanticCustomError(
                "phase_3_prerequisites",
                "Phase-3 prerequisites are parent gate result hashes only",
            )
        if len(self.parent_gate_result_hashes) != PHASE_3_PARENT_COUNT:
            raise PydanticCustomError(
                "phase_3_parent",
                "Phase-3 requires exactly one parent gate result hash (phase-2)",
            )
        if self.golden_sha256 is None:
            raise PydanticCustomError(
                "phase_3_golden",
                "Phase-3 fixtures require the independent Golden index binding",
            )
        if self.toolchain_lock_sha256 is None or self.fixture_manifest_sha256 is None:
            raise PydanticCustomError(
                "phase_3_bindings",
                "Phase-3 requires the toolchain lock and fixture manifest bindings",
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
