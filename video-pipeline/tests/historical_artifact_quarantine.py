from __future__ import annotations

from typing import Final

MISSING_ATTEMPT_SHA256: Final = (
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
QUARANTINED_ON: Final = "2026-09-02"
HISTORICAL_ARTIFACT_SKIP_REASON: Final = (
    "deleted historical gate evidence bytes are unavailable; "
    f"quarantined {QUARANTINED_ON}; missing attempt sha256={MISSING_ATTEMPT_SHA256}"
)
HISTORICAL_ARTIFACT_NODE_IDS: Final = (
    "tests/fixtures/test_phase0a.py::test_publish_recovers_policy_only_and_both_states",
    "tests/fixtures/test_phase0a.py::test_publish_recovers_temp_only_state",
    "tests/fixtures/test_phase0a.py::test_noncanonical_prepared_policy_is_rejected",
    "tests/fixtures/test_phase0a.py::test_corrupt_intent_copy_is_rejected",
    "tests/fixtures/test_phase0a.py::test_incomplete_final_ledger_fragment_is_repaired",
    "tests/fixtures/test_phase0a.py::test_complete_invalid_ledger_tail_blocks_recovery",
    "tests/fixtures/test_refreeze_versions.py::test_phase0a_v2_refreeze_preparation",
    "tests/fixtures/test_refreeze_versions.py::test_same_version_refreeze_still_refused",
    "tests/fixtures/test_refreeze_versions.py::test_v1_policy_path_stays_refused",
    "tests/gates/test_control_plane_inputs.py::test_frozen_policy_is_canonical_and_bound_to_the_0c_parent",
    "tests/gates/test_control_plane_inputs.py::test_freeze_receipt_binds_the_six_control_plane_fixtures",
    "tests/gates/test_control_plane_inputs.py::test_policy_verification_passes_with_frozen_artifacts",
    "tests/gates/test_control_plane_inputs.py::test_post_result_policy_edit_is_rejected",
    "tests/gates/test_control_plane_inputs.py::test_stale_parent_gate_result_is_rejected_at_prepare_time",
    "tests/gates/test_phase0a_policy.py::test_freeze_receipt_matches_policy_bytes",
    "tests/gates/test_phase0a_policy.py::test_post_result_policy_edit_is_rejected",
    "tests/gates/test_phase0c_policy.py::test_frozen_policy_is_canonical_and_bound_to_the_0b_parent",
    "tests/gates/test_phase0c_policy.py::test_freeze_receipt_matches_policy_bytes",
    "tests/gates/test_phase0c_policy.py::test_policy_verification_passes_with_frozen_artifacts",
    "tests/gates/test_phase0c_policy.py::test_post_result_policy_edit_is_rejected",
    "tests/gates/test_phase0c_policy.py::test_stale_parent_gate_result_is_rejected_at_prepare_time",
    "tests/gates/test_phase1_inputs.py::test_frozen_policy_is_canonical_with_two_parents",
    "tests/gates/test_phase1_inputs.py::test_freeze_receipt_binds_five_fixtures_two_parents_and_lock",
    "tests/gates/test_phase1_inputs.py::test_policy_verification_passes_with_frozen_artifacts",
    "tests/gates/test_phase1_inputs.py::test_lock_carries_passed_whisper_and_editorial_model_smokes",
    "tests/gates/test_phase1_inputs.py::test_edited_policy_is_rejected_by_receipt_binding",
    "tests/gates/test_phase1_inputs.py::test_stale_parent_gate_result_is_rejected_at_prepare_time",
    "tests/gates/test_phase1_inputs.py::test_failed_parent_gate_result_is_rejected",
    "tests/gates/test_phase2_inputs.py::test_frozen_policy_is_canonical_with_three_parents_and_h1_prerequisite",
    "tests/gates/test_phase2_inputs.py::test_freeze_receipt_binds_five_fixtures_three_parents_lock_and_checkpoint",
    "tests/gates/test_phase2_inputs.py::test_policy_verification_passes_with_frozen_artifacts",
    "tests/gates/test_phase2_inputs.py::test_edited_policy_is_rejected_by_receipt_binding",
    "tests/gates/test_phase2_inputs.py::test_synthetic_checkpoint_is_refused_at_prepare_time",
    "tests/gates/test_phase2_inputs.py::test_stale_parent_gate_result_is_refused_at_prepare_time",
    "tests/gates/test_phase3_inputs.py::test_frozen_policy_is_canonical_with_single_phase2_parent",
    "tests/gates/test_phase3_inputs.py::test_freeze_receipt_binds_two_brand_fixtures_parent_lock_and_contract",
    "tests/gates/test_phase3_inputs.py::test_policy_verification_passes_with_frozen_artifacts",
    "tests/gates/test_phase3_inputs.py::test_edited_policy_is_rejected_by_receipt_binding",
    "tests/gates/test_phase3_inputs.py::test_stale_parent_gate_result_is_refused_at_prepare_time",
    "tests/phase0b/test_freeze.py::test_stale_failing_parent_result_is_rejected",
    "tests/phase0b/test_freeze.py::test_tampered_parent_policy_binding_is_rejected",
    "tests/phase0b/test_freeze.py::test_publish_recovers_policy_only_and_both_states",
    "tests/phase0b/test_freeze.py::test_corrupt_intent_copy_is_rejected",
    "tests/phase0b/test_freeze.py::test_intent_append_is_idempotent_for_identical_event",
    "tests/phase0c/test_freeze.py::test_tampered_parent_policy_binding_is_rejected",
    "tests/phase1/test_gate.py::test_00_all_five_criteria_pass",
    "tests/phase1/test_gate.py::test_10_every_criterion_is_bound_to_real_evidence",
    "tests/phase1/test_gate.py::test_20_h1_waiting_marker_stops_the_flow",
    "tests/phase1/test_gate.py::test_30_marker_binds_bundles_and_previews",
    "tests/phase1/test_gate.py::test_40_structured_coverage_meets_threshold_from_declared_commands",
    "tests/phase1/test_gate.py::test_50_correction_sequence_final_state_matches_goldens",
    "tests/phase1/test_gate.py::test_60_gate_result_binds_the_evidence_tree",
    "tests/phase1/test_gate.py::test_70_marker_bytes_are_canonical",
)
HISTORICAL_ARTIFACT_NODE_ID_SET: Final = frozenset(HISTORICAL_ARTIFACT_NODE_IDS)
