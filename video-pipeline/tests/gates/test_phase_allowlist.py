from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from services.gates import PHASE_0A_CRITERIA, GatePolicy

if TYPE_CHECKING:
    from tests.gates.support import PolicyPayloadFactory


def test_missing_stop_criterion_is_rejected(
    phase_0a_policy_payload: PolicyPayloadFactory,
) -> None:
    payload = phase_0a_policy_payload()
    payload["criteria"] = [
        criterion
        for criterion in PHASE_0A_CRITERIA
        if criterion != "phase-0a-stop-mvp-capability-unavailable"
    ]

    with pytest.raises(ValidationError, match="phase_0a_criteria"):
        GatePolicy.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "capability",
    [
        "editorial_selection",
        "whisper_asr",
        "channel_profiles",
        "keyword_overlay",
        "deterministic_qc",
    ],
)
def test_later_phase_capability_is_rejected_from_0a_allowlist(
    phase_0a_policy_payload: PolicyPayloadFactory,
    capability: str,
) -> None:
    payload = phase_0a_policy_payload()
    capabilities = payload["capability_allowlist"]
    assert isinstance(capabilities, list)
    payload["capability_allowlist"] = [*capabilities, capability]

    with pytest.raises(ValidationError, match="phase_0a_capabilities"):
        GatePolicy.model_validate_json(json.dumps(payload))


def test_nonempty_parent_list_is_rejected_for_0a_draft(
    phase_0a_policy_payload: PolicyPayloadFactory,
) -> None:
    payload = phase_0a_policy_payload()
    payload["parent_gate_result_hashes"] = ["a" * 64]

    with pytest.raises(ValidationError, match="phase_0a_prerequisites"):
        GatePolicy.model_validate_json(json.dumps(payload))


def test_nonempty_binding_list_is_rejected_for_0a_draft(
    phase_0a_policy_payload: PolicyPayloadFactory,
) -> None:
    payload = phase_0a_policy_payload()
    payload["prerequisite_bindings"] = [
        {"kind": "gate_result", "gate_id": "bootstrap", "result_sha256": "a" * 64}
    ]

    with pytest.raises(ValidationError, match="phase_0a_prerequisites"):
        GatePolicy.model_validate_json(json.dumps(payload))
