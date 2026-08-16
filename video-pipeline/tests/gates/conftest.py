from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from services.gates import PHASE_0A_CAPABILITIES, PHASE_0A_CRITERIA

if TYPE_CHECKING:
    from tests.gates.support import PolicyPayload, PolicyPayloadFactory


@pytest.fixture
def phase_0a_policy_payload() -> PolicyPayloadFactory:
    def build() -> PolicyPayload:
        return {
            "schema_version": "gate-policy-v1",
            "record_type": "gate_policy",
            "purpose": "gate_policy_freeze",
            "gate_id": "phase-0a",
            "gate_version": "v1",
            "parent_gate_result_hashes": [],
            "criteria": list(PHASE_0A_CRITERIA),
            "toolchain_lock_sha256": None,
            "fixture_manifest_sha256": None,
            "golden_sha256": None,
            "prerequisite_bindings": [],
            "capability_allowlist": list(PHASE_0A_CAPABILITIES),
        }

    return build
