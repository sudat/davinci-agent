from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from services.contracts.export_schemas import export_gate_schemas
from services.gates import (
    GatePolicy,
    GateResult,
    OperatorCheckpointBinding,
    canonical_gate_bytes,
)

if TYPE_CHECKING:
    from tests.gates.support import PolicyPayloadFactory


def test_0a_draft_validates_when_results_and_bindings_are_unfilled(
    phase_0a_policy_payload: PolicyPayloadFactory,
) -> None:
    payload = phase_0a_policy_payload()

    policy = GatePolicy.model_validate_json(json.dumps(payload))

    assert policy.parent_gate_result_hashes == ()
    assert policy.prerequisite_bindings == ()
    assert policy.toolchain_lock_sha256 is None
    assert policy.fixture_manifest_sha256 is None
    assert policy.golden_sha256 is None


def test_canonical_bytes_are_stable_for_equivalent_policy_input(
    phase_0a_policy_payload: PolicyPayloadFactory,
) -> None:
    first_payload = phase_0a_policy_payload()
    second_payload = dict(reversed(tuple(phase_0a_policy_payload().items())))

    first_bytes = canonical_gate_bytes(GatePolicy.model_validate_json(json.dumps(first_payload)))
    second_bytes = canonical_gate_bytes(
        GatePolicy.model_validate_json(json.dumps(second_payload))
    )

    assert first_bytes == second_bytes
    assert b" " not in first_bytes


@pytest.mark.parametrize("value", [1.25, math.nan], ids=["float", "nan"])
def test_float_is_rejected_when_nested_anywhere_in_policy(
    phase_0a_policy_payload: PolicyPayloadFactory,
    value: float,
) -> None:
    payload = phase_0a_policy_payload()
    payload["criteria"] = ["phase-0a-required-fixtures", value]

    with pytest.raises(ValidationError, match="float_forbidden"):
        GatePolicy.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("field", ["signature", "trust_root_sha256"])
def test_signature_like_field_is_rejected(
    phase_0a_policy_payload: PolicyPayloadFactory,
    field: str,
) -> None:
    payload = phase_0a_policy_payload()
    payload[field] = "a" * 64

    with pytest.raises(ValidationError, match="extra_forbidden"):
        GatePolicy.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "field",
    ["passed", "criteria_results", "evidence_bundles", "observed_results"],
)
def test_prefilled_observed_result_is_rejected_from_policy(
    phase_0a_policy_payload: PolicyPayloadFactory,
    field: str,
) -> None:
    payload = phase_0a_policy_payload()
    payload[field] = []

    with pytest.raises(ValidationError, match="extra_forbidden"):
        GatePolicy.model_validate_json(json.dumps(payload))


def test_unknown_prerequisite_kind_is_rejected(
    phase_0a_policy_payload: PolicyPayloadFactory,
) -> None:
    payload = phase_0a_policy_payload()
    payload["prerequisite_bindings"] = [{"kind": "checkpoint"}]

    with pytest.raises(ValidationError, match="union_tag_invalid"):
        GatePolicy.model_validate_json(json.dumps(payload))


def test_operator_checkpoint_requires_all_target_and_receipt_hashes(
    phase_0a_policy_payload: PolicyPayloadFactory,
) -> None:
    payload = phase_0a_policy_payload()
    payload["gate_id"] = "phase-test"
    payload["prerequisite_bindings"] = [
        {
            "kind": "operator_checkpoint",
            "purpose": "EDITORIAL_APPROVED",
            "episode_id": "episode-1",
            "target_plan_sha256": "a" * 64,
        }
    ]

    with pytest.raises(ValidationError, match="Field required"):
        GatePolicy.model_validate_json(json.dumps(payload))


def test_complete_operator_checkpoint_is_parsed_as_distinct_binding(
    phase_0a_policy_payload: PolicyPayloadFactory,
) -> None:
    payload = phase_0a_policy_payload()
    payload["gate_id"] = "phase-review"
    payload["prerequisite_bindings"] = [
        {
            "kind": "operator_checkpoint",
            "purpose": "EDITORIAL_APPROVED",
            "episode_id": "episode-1",
            "target_plan_sha256": "a" * 64,
            "target_timeline_ir_sha256": "b" * 64,
            "target_preview_sha256": "c" * 64,
            "operation_record_sha256": "d" * 64,
            "checkpoint_sha256": "e" * 64,
        }
    ]

    policy = GatePolicy.model_validate_json(json.dumps(payload))

    assert isinstance(policy.prerequisite_bindings[0], OperatorCheckpointBinding)


def test_operator_checkpoint_cannot_be_used_as_parent_gate_result_hash(
    phase_0a_policy_payload: PolicyPayloadFactory,
) -> None:
    payload = phase_0a_policy_payload()
    payload["parent_gate_result_hashes"] = [
        {"kind": "operator_checkpoint", "checkpoint_sha256": "a" * 64}
    ]

    with pytest.raises(ValidationError):
        GatePolicy.model_validate_json(json.dumps(payload))


def test_gate_result_rejects_self_attested_pass_without_raw_evidence() -> None:
    payload = {
        "schema_version": "gate-result-v1",
        "record_type": "gate_result",
        "gate_id": "phase-0a",
        "gate_version": "v1",
        "policy_sha256": "a" * 64,
        "passed": True,
        "evidence_bundles": [],
        "criteria_results": [],
    }

    with pytest.raises(ValidationError, match="too_short"):
        GateResult.model_validate_json(json.dumps(payload))


def test_gate_result_rejects_pass_that_disagrees_with_criterion_results() -> None:
    payload = {
        "schema_version": "gate-result-v1",
        "record_type": "gate_result",
        "gate_id": "phase-0a",
        "gate_version": "v1",
        "policy_sha256": "a" * 64,
        "passed": True,
        "evidence_bundles": [
            {"bundle_sha256": "b" * 64, "raw_evidence_sha256s": ["c" * 64]}
        ],
        "criteria_results": [
            {
                "criterion_id": "phase-0a-frame-delta-zero",
                "passed": False,
                "raw_evidence_sha256s": ["d" * 64],
            }
        ],
    }

    with pytest.raises(ValidationError, match="gate_pass_mismatch"):
        GateResult.model_validate_json(json.dumps(payload))


def test_gate_schema_drift_is_detected_when_exported_copy_is_tampered(
    tmp_path: Path,
) -> None:
    output = tmp_path / "gates"
    export_gate_schemas(output, check=False)
    (output / "gate-policy.json").write_text("{}\n")

    in_sync = export_gate_schemas(output, check=True)

    assert in_sync is False
