"""Offline unit tests for the Phase-0B gate logic (todo-25).

Happy paths use the synthetic conform worlds (pure computation, no media) and
the frozen manifest/Golden tables; failure paths inject the five canonical
faults (off-by-one golden, missing duplicate report, >1-frame sync, stale
Resolve build, direct VFR-original edit) into fake evidence trees and require
the real evaluator to reject them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from services.conform.convert import frame_conversion_accounting
from services.contracts.primitives import RationalFrameRate, SourceFrameSpan
from services.fixtures.manifest_phase0b import Phase0BFixtureManifest
from services.foundation_io import sha256_file
from services.gates import GatePolicy
from services.gates.phase0b import PHASE_0B_CRITERIA, PHASE_0B_VARIANTS
from services.normalize.models import FileIdentity
from services.spike.gate_phase0b_evaluate import (
    Evaluate0bInputs,
    Evaluate0bOutcome,
    GoldenLoadError,
    evaluate0b,
    load_golden,
)
from services.spike.gate_phase0b_fakes import (
    FaultKnobs0b,
    happy_sync_rows,
    load_world,
    refusal_label,
    synthesize0b,
)
from services.spike.gate_phase0b_models import (
    CRITERION_ANCHORS,
    CRITERION_DROPS,
    CRITERION_GOLDEN,
    CRITERION_READBACK,
    CRITERION_SYNC,
    STOP_STALE_BINDING,
    SYNC_NAME,
    LiveReadbackReport,
    SyncMeasurements,
    fixture_manifest_path,
)
from services.spike.gate_phase0b_readback import (
    ReadbackRefusedError,
    build_placements,
    require_edit_source,
)
from services.spike.gate_phase0b_sync import source_span
from services.spike.stop_rules import record_stop, stop_marker_path, stop_recorded
from services.toolchain.models import Phase0BToolchainLock, load_lock

POLICY = Path("config/gates/phase-0b-v2.json")
LOCK = Path("config/toolchains/phase-0b-v1.json")
GOLDEN_EXPECTED = Path("tests/goldens/reference/phase-0b/expected.json")


def _parent_result() -> Path:
    lock = load_lock(LOCK)
    assert isinstance(lock, Phase0BToolchainLock)
    candidate = Path(lock.resolve.report_path).resolve().parent / "phase-0a" / "gate-result.json"
    if not candidate.is_file():
        pytest.skip(f"frozen phase-0a gate result not bootstrapped: {candidate}")
    return candidate


def _evaluate(tmp_path: Path, knobs: FaultKnobs0b) -> Evaluate0bOutcome:
    policy = GatePolicy.model_validate_json(POLICY.read_bytes())
    evidence = tmp_path / "phase-0b"
    host = synthesize0b(evidence, knobs)
    return evaluate0b(
        Evaluate0bInputs(
            policy=policy,
            policy_sha256=sha256_file(POLICY),
            evidence=evidence,
            host_report_path=host,
            parent_result_path=_parent_result(),
        )
    )


@pytest.mark.parametrize("variant", PHASE_0B_VARIANTS)
def test_conform_model_reproduces_manifest_and_golden_tables(variant: str) -> None:
    fixture = Phase0BFixtureManifest.model_validate_json(
        fixture_manifest_path(variant).read_bytes()
    )
    golden = json.loads(GOLDEN_EXPECTED.read_bytes())["variants"][variant]
    span = SourceFrameSpan(
        start_frame=0,
        end_frame=fixture.conversions["cfr30"].input_frames,
        rate=RationalFrameRate(
            num=fixture.rational_frame_rate_table.frame_rate.num,
            den=fixture.rational_frame_rate_table.frame_rate.den,
        ),
    )
    targets: tuple[tuple[Literal["cfr24", "cfr30"], RationalFrameRate], ...] = (
        ("cfr24", RationalFrameRate(num=24, den=1)),
        ("cfr30", RationalFrameRate(num=30, den=1)),
    )
    for target, rate in targets:
        computed = frame_conversion_accounting(span, rate)
        frozen = fixture.conversions[target]
        table = golden[target]
        assert computed.output_frames == frozen.output_frames == table["output_frames"]
        assert list(computed.dropped_source_frames) == list(frozen.dropped_source_frames)
        assert list(computed.dropped_source_frames) == list(table["dropped_source_frames"])
        assert list(computed.duplicated_source_frames) == list(frozen.duplicated_source_frames)
        assert list(computed.duplicated_source_frames) == list(table["duplicated_source_frames"])


@pytest.mark.parametrize("variant", PHASE_0B_VARIANTS)
def test_happy_sync_rows_all_pass(variant: str) -> None:
    rows = happy_sync_rows(variant)
    assert rows
    assert all(row.passed for row in rows), [row.name for row in rows if not row.passed]
    assert any(row.kind == "exact-zero" for row in rows)
    assert any(row.kind == "tolerance-one-frame" for row in rows)


def test_happy_fake_tree_passes_all_criteria(tmp_path: Path) -> None:
    outcome = _evaluate(tmp_path, FaultKnobs0b())
    assert outcome.result.passed is True
    assert outcome.stop_triggered is False
    assert outcome.variants_passed == PHASE_0B_VARIANTS
    assert tuple(row.criterion_id for row in outcome.result.criteria_results) == (
        PHASE_0B_CRITERIA
    )
    sync = SyncMeasurements.model_validate_json(
        (tmp_path / "phase-0b" / SYNC_NAME).read_bytes()
    )
    assert sync.all_passed is True


def test_off_by_one_golden_fails(tmp_path: Path) -> None:
    outcome = _evaluate(tmp_path, FaultKnobs0b(off_by_one_golden=True))
    assert outcome.result.passed is False
    codes = {row.code for row in outcome.mismatches}
    assert "off-by-one-golden" in codes
    by_id = {row.criterion_id: row.passed for row in outcome.result.criteria_results}
    assert by_id[CRITERION_GOLDEN] is False


def test_missing_duplicate_report_fails(tmp_path: Path) -> None:
    outcome = _evaluate(tmp_path, FaultKnobs0b(missing_duplicate_report=True))
    assert outcome.result.passed is False
    codes = {row.code for row in outcome.mismatches}
    assert "missing-duplicate-report" in codes
    by_id = {row.criterion_id: row.passed for row in outcome.result.criteria_results}
    assert by_id[CRITERION_DROPS] is False


def test_sync_over_one_frame_fails(tmp_path: Path) -> None:
    outcome = _evaluate(tmp_path, FaultKnobs0b(sync_over_one_frame=True))
    assert outcome.result.passed is False
    codes = {row.code for row in outcome.mismatches}
    assert "sync-over-one-frame" in codes
    by_id = {row.criterion_id: row.passed for row in outcome.result.criteria_results}
    assert by_id[CRITERION_SYNC] is False


def test_stale_resolve_build_stops_and_marker_refuses_rerun(tmp_path: Path) -> None:
    outcome = _evaluate(tmp_path, FaultKnobs0b(stale_resolve_build=True))
    assert outcome.result.passed is False
    assert outcome.stop_triggered is True
    assert outcome.stop_criterion == STOP_STALE_BINDING
    by_id = {row.criterion_id: row.passed for row in outcome.result.criteria_results}
    assert by_id[CRITERION_READBACK] is False
    marker = record_stop(
        tmp_path / "phase-0b",
        outcome.stop_criterion,
        outcome.stop_reason,
        gate_id="phase-0b",
    )
    assert marker == stop_marker_path(tmp_path / "phase-0b")
    recorded = stop_recorded(tmp_path / "phase-0b")
    assert recorded is not None
    assert recorded.gate_id == "phase-0b"


def test_vfr_original_is_refused_for_resolve_edit(tmp_path: Path) -> None:
    fixture = Phase0BFixtureManifest.model_validate_json(
        fixture_manifest_path("p0b-vfr-2-3-cadence").read_bytes()
    )
    world = load_world(fixture)
    original = Path(world.manifest.file.path)
    with pytest.raises(ReadbackRefusedError, match="never placed into Resolve edit paths"):
        require_edit_source(original, world.record)
    assert refusal_label(world.record, original) == "original_refused_for_resolve_edit"


def test_edit_source_guard_accepts_only_hash_matching_output(tmp_path: Path) -> None:
    fixture = Phase0BFixtureManifest.model_validate_json(
        fixture_manifest_path("p0b-cfr24").read_bytes()
    )
    world = load_world(fixture)
    media = tmp_path / "edit-source.mov"
    media.write_bytes(b"fake-edit-source")
    record = world.record.model_copy(
        update={
            "output": FileIdentity(
                path=str(media), sha256=sha256_file(media), size_bytes=media.stat().st_size
            )
        }
    )
    assert require_edit_source(media, record) == media.resolve()
    media.write_bytes(b"tampered")
    with pytest.raises(ReadbackRefusedError, match="no longer hashes"):
        require_edit_source(media, record)


def test_build_placements_covers_every_frozen_marker_in_order() -> None:
    fixture = Phase0BFixtureManifest.model_validate_json(
        fixture_manifest_path("p0b-cfr24").read_bytes()
    )
    output_frames = fixture.conversions["cfr30"].output_frames
    placements = build_placements(fixture, output_frames)
    assert placements[0].item_id == "whole-clip"
    assert (placements[0].source_start, placements[0].source_end) == (0, output_frames)
    assert tuple(p.item_id for p in placements[1:]) == tuple(
        marker.marker_id for marker in fixture.resolve_readback
    )
    for placement, marker in zip(placements[1:], fixture.resolve_readback, strict=True):
        assert marker.cfr30_frame is not None
        assert placement.source_start == marker.cfr30_frame
        expected_end = min(marker.cfr30_frame + 30, output_frames)  # ANCHOR_WINDOW_FRAMES
        assert placement.source_end == expected_end
        assert placement.source_end > placement.source_start


def test_golden_load_rejects_policy_binding_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.spike.gate_phase0b_evaluate.GOLDEN_DIR", Path("tests"))
    with pytest.raises((GoldenLoadError, OSError)):
        load_golden("0" * 64)


def test_readback_model_rejects_malformed_payload() -> None:

    with pytest.raises(ValidationError):
        LiveReadbackReport.model_validate_json(b'{"schema_version": "nope"}')


def test_criterion_constants_are_the_frozen_five() -> None:
    assert (
        CRITERION_GOLDEN,
        CRITERION_DROPS,
        CRITERION_ANCHORS,
        CRITERION_READBACK,
        CRITERION_SYNC,
    ) == PHASE_0B_CRITERIA


def test_source_span_matches_rational_table() -> None:
    fixture = Phase0BFixtureManifest.model_validate_json(
        fixture_manifest_path("p0b-ntsc2997").read_bytes()
    )
    span = source_span(fixture)
    assert span.length == 600
    assert (span.rate.num, span.rate.den) == (30000, 1001)
