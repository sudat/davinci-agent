"""Round-trip + refusal tests for the v44 gate records (T14/T15/T16).

T14: the BLOCKED record's empty-missing lie is unrepresentable.
T15: ``V1ObservationRecord`` (``v44-1-observation-v1``) must round-trip
canonically; seed-refusal and validation refusals are in
``tests/cli/test_v44_observe.py`` — the model-level refusals live here.
T16: ``V44GateSummaryV1`` (``v44-gate-summary-v1``) round-trips and each
anti-fabrication refusal path (blocked domain / missing-or-non-publishable
verdict / QC blocked / missing AHT) is unrepresentable with passed=true.
"""

from __future__ import annotations

import json
from pathlib import Path

import pydantic
import pytest

from services.metrics.v44_gate_state import (
    V1ObservationRecord,
    V44GateBlockedV1,
    V44GateSummaryV1,
    write_blocked_record,
    write_observation_record,
    write_v44_2_summary,
)

VALID: dict[str, object] = {
    "gate": "V44-0",
    "reason": "operator-needed",
    "missing": ["real-footage", "operator-ground-truth"],
    "operator_instructions": (
        "private/reference-episodes/v44-real-01/README.md と "
        "video-pipeline/docs/runbooks/v44-first-publish.md を参照"
    ),
    "checked_at": "2026-08-24T00:00:00+00:00",
    "commit_sha": "958bf2e3720a806919db0a93f1ac1be29c553521",
}


def test_round_trip(tmp_path: Path) -> None:
    record = V44GateBlockedV1.model_validate(VALID)
    out = tmp_path / "v44-0" / "BLOCKED.json"
    write_blocked_record(out, record)
    reread = V44GateBlockedV1.model_validate_json(out.read_text(encoding="utf-8"))
    assert reread == record
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema_version"] == "v44-gate-blocked-v1"
    assert data["gate"] == "V44-0"
    assert data["missing"] == ["real-footage", "operator-ground-truth"]


def test_empty_missing_is_unrepresentable() -> None:
    payload = {**VALID, "missing": []}
    with pytest.raises(pydantic.ValidationError):
        V44GateBlockedV1.model_validate(payload)


def test_extra_field_rejected() -> None:
    payload = {**VALID, "simulated": True}
    with pytest.raises(pydantic.ValidationError):
        V44GateBlockedV1.model_validate(payload)


def test_unknown_gate_rejected() -> None:
    payload = {**VALID, "gate": "V44-3"}
    with pytest.raises(pydantic.ValidationError):
        V44GateBlockedV1.model_validate(payload)


def test_only_operator_needed_reason_accepted() -> None:
    payload = {**VALID, "reason": "engineer-lazy"}
    with pytest.raises(pydantic.ValidationError):
        V44GateBlockedV1.model_validate(payload)


def test_asr_alignment_failed_reason_accepted_and_old_payloads_parse() -> None:
    """Task 3 additive extension: blocked records may carry
    ``asr-alignment-failed``; historical ``operator-needed`` files and their
    on-disk bytes parse unchanged."""
    blocked = V44GateBlockedV1.model_validate(
        {**VALID, "reason": "asr-alignment-failed"}
    )
    assert blocked.reason == "asr-alignment-failed"
    historical = V44GateBlockedV1.model_validate_json(
        json.dumps({**VALID, "reason": "operator-needed"})
    )
    assert historical.reason == "operator-needed"
    with pytest.raises(pydantic.ValidationError):
        V44GateBlockedV1.model_validate({**VALID, "reason": "cer-threshold-changed"})


def test_bad_commit_sha_rejected() -> None:
    payload = {**VALID, "commit_sha": "short"}
    with pytest.raises(pydantic.ValidationError):
        V44GateBlockedV1.model_validate(payload)


# ---------------------------------------------------------------------------
# V44-1 observation model (T15)
# ---------------------------------------------------------------------------

VALID_OBSERVATION: dict[str, object] = {
    "episode_id": "ep-abc123",
    "stage_timeline": {
        "job_status": "PREVIEW_READY",
        "current_stage": "preview",
        "runs": [
            {
                "stage_name": "preview",
                "status": "succeeded",
                "retry_count": 0,
                "idempotency_key": "cockpit-episode-runner-v1:550e8400:preview",
            }
        ],
    },
    "ttfrp_seconds": 42.5,
    "corrections": [
        {
                "text": "この後2秒残して",
                "at_seconds": 1.0,
                "applied": True,
                "rebuild_wall_clock_seconds": 1.42,
            }
    ],
    "rebuild_records": [
        {
            "sequence": 1,
            "applied_command": "cmd-keep-longer-001",
            "stages": ["plan", "compile", "preview"],
            "rebuild_wall_clock_seconds": 1.42,
            "unrelated_stages_skipped": ["ingest"],
            "at": "2026-08-23T00:00:00+00:00",
        }
    ],
    "operator_note": None,
    "internal_path_leak": False,
    "observed_at": "2026-08-23T00:00:00+00:00",
}


def test_observation_round_trip(tmp_path: Path) -> None:
    record = V1ObservationRecord.model_validate(VALID_OBSERVATION)
    out = tmp_path / "obs" / "observation.json"
    write_observation_record(out, record)
    reread = V1ObservationRecord.model_validate_json(out.read_text(encoding="utf-8"))
    assert reread == record
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema_version"] == "v44-1-observation-v1"
    assert data["episode_id"] == "ep-abc123"
    assert data["internal_path_leak"] is False


def test_observation_extra_field_rejected() -> None:
    payload = {**VALID_OBSERVATION, "simulated": True}
    with pytest.raises(pydantic.ValidationError):
        V1ObservationRecord.model_validate(payload)


def test_observation_negative_ttfrp_rejected() -> None:
    payload = {**VALID_OBSERVATION, "ttfrp_seconds": -1.0}
    with pytest.raises(pydantic.ValidationError):
        V1ObservationRecord.model_validate(payload)


def test_observation_bad_job_status_rejected() -> None:
    payload = {
        **VALID_OBSERVATION,
        "stage_timeline": {
            "job_status": "NOT_A_STATUS",
            "current_stage": "preview",
            "runs": [],
        },
    }
    with pytest.raises(pydantic.ValidationError):
        V1ObservationRecord.model_validate(payload)


def test_observation_null_ttfrp_allowed() -> None:
    payload = {**VALID_OBSERVATION, "ttfrp_seconds": None}
    record = V1ObservationRecord.model_validate(payload)
    assert record.ttfrp_seconds is None


def test_observation_operator_note_round_trip(tmp_path: Path) -> None:
    payload = {**VALID_OBSERVATION, "operator_note": "撮影の雰囲気は良い"}
    record = V1ObservationRecord.model_validate(payload)
    out = tmp_path / "obs2" / "observation.json"
    write_observation_record(out, record)
    reread = V1ObservationRecord.model_validate_json(out.read_text(encoding="utf-8"))
    assert reread.operator_note == "撮影の雰囲気は良い"


# ---------------------------------------------------------------------------
# V44-2 gate summary model (T16)
# ---------------------------------------------------------------------------

PASSED_SUMMARY: dict[str, object] = {
    "gate": "V44-2",
    "passed": True,
    "operator_verdict": "publishable",
    "blocked_domains": [],
    "technical_qc": "passed",
    "editorial_qc_blocked_items": 0,
    "bootstrap_aht_minutes": 30.5,
    "direct_resolve_minutes": 12.0,
    "director_pin_model": "gpt-5.6-sol",
    "evidence_pins": {"analysis_provider": "whisper-cpp-cli:1fc70f77bc69"},
    "finishing_report_ref": "finishing/finishing-run.json",
    "subtitle_proof_ref": "finishing/subtitle-proof/subtitle-proof.json",
    "artifacts": ["finishing/finishing-run.json", "review/publishability.json"],
    "recorded_at": "2026-08-24T00:00:00Z",
    "commit_sha": "958bf2e3720a806919db0a93f1ac1be29c553521",
}


def test_gate_summary_round_trip(tmp_path: Path) -> None:
    record = V44GateSummaryV1.model_validate(PASSED_SUMMARY)
    out = tmp_path / "v44-2" / "gate-summary.json"
    write_v44_2_summary(out, record)
    reread = V44GateSummaryV1.model_validate_json(out.read_text(encoding="utf-8"))
    assert reread == record
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema_version"] == "v44-gate-summary-v1"
    assert data["gate"] == "V44-2"
    assert data["passed"] is True
    assert data["bootstrap_aht_minutes"] == 30.5


def test_gate_summary_failed_state_representable() -> None:
    payload = {
        **PASSED_SUMMARY,
        "passed": False,
        "operator_verdict": "not_publishable",
        "blocked_domains": ["delivery_qc"],
        "technical_qc": None,
        "editorial_qc_blocked_items": None,
        "bootstrap_aht_minutes": None,
        "direct_resolve_minutes": None,
    }
    record = V44GateSummaryV1.model_validate(payload)
    assert record.passed is False
    assert record.blocked_domains == ("delivery_qc",)


def test_passed_with_blocked_domain_unrepresentable() -> None:
    payload = {**PASSED_SUMMARY, "blocked_domains": ["subtitle"]}
    with pytest.raises(pydantic.ValidationError):
        V44GateSummaryV1.model_validate(payload)


def test_passed_with_missing_verdict_unrepresentable() -> None:
    payload = {**PASSED_SUMMARY, "operator_verdict": None}
    with pytest.raises(pydantic.ValidationError):
        V44GateSummaryV1.model_validate(payload)


def test_passed_with_not_publishable_verdict_unrepresentable() -> None:
    payload = {**PASSED_SUMMARY, "operator_verdict": "not_publishable"}
    with pytest.raises(pydantic.ValidationError):
        V44GateSummaryV1.model_validate(payload)


def test_passed_with_technical_qc_blocked_unrepresentable() -> None:
    payload = {**PASSED_SUMMARY, "technical_qc": "blocked"}
    with pytest.raises(pydantic.ValidationError):
        V44GateSummaryV1.model_validate(payload)


def test_passed_with_editorial_qc_blocked_unrepresentable() -> None:
    payload = {**PASSED_SUMMARY, "editorial_qc_blocked_items": 2}
    with pytest.raises(pydantic.ValidationError):
        V44GateSummaryV1.model_validate(payload)


def test_passed_with_missing_aht_unrepresentable() -> None:
    payload = {**PASSED_SUMMARY, "bootstrap_aht_minutes": None}
    with pytest.raises(pydantic.ValidationError):
        V44GateSummaryV1.model_validate(payload)


def test_passed_with_missing_direct_resolve_minutes_unrepresentable() -> None:
    payload = {**PASSED_SUMMARY, "direct_resolve_minutes": None}
    with pytest.raises(pydantic.ValidationError):
        V44GateSummaryV1.model_validate(payload)


def test_gate_summary_extra_field_rejected() -> None:
    payload = {**PASSED_SUMMARY, "simulated": True}
    with pytest.raises(pydantic.ValidationError):
        V44GateSummaryV1.model_validate(payload)
