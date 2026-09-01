"""Parity harness scaffold — legacy-vs-legacy self-checks.

Stage 1 covers only the legacy backend; the MCP backend is stubbed
until task 38/39.  The fixture under tests/qa/fixtures/parity-basecut/
is the canonical base-cut IR that task 11 will reuse.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from services.contracts.timeline_ir import TimelineIrProduction
from services.foundation_io import sha256_file
from services.qa.parity_harness import (
    _build_legacy_structure,
    diff_structures,
    extract_structure,
    run_parity,
)
from services.resolve_adapter.models import MediaBinding
from services.resolve_adapter.package import PackageCompileRequest, compile_resolve_package
from services.toolchain.models import Phase2ToolchainLock, load_lock

FIXTURE_DIR = Path(__file__).parent / "fixtures/parity-basecut"
IR_PATH = FIXTURE_DIR / "ir.json"
EXPECTED_PATH = FIXTURE_DIR / "expected_extract.json"


def _load_fixture_ir() -> TimelineIrProduction:
    payload: object = json.loads(IR_PATH.read_bytes())
    assert isinstance(payload, dict)
    return TimelineIrProduction.model_validate(payload, strict=False)


def _expected_extract() -> dict[str, object]:
    payload: object = json.loads(EXPECTED_PATH.read_bytes())
    assert isinstance(payload, dict)
    return payload


def test_legacy_vs_legacy_zero_diff(tmp_path: Path) -> None:
    """Given: the same canonical IR.

    When: both backends are legacy.
    Then: the diff is empty and the report is written atomically.
    """

    # Given: the canonical fixture IR on disk
    assert IR_PATH.is_file()

    # When: run_parity with legacy vs legacy
    output = tmp_path / "parity-report.json"
    report = run_parity(IR_PATH, backend_a="legacy", backend_b="legacy", output_path=output)

    # Then: zero differences, summaries identical, report file written
    assert report["differences"] == []
    assert report["a_summary"] == report["b_summary"]
    assert output.is_file()
    written: object = json.loads(output.read_bytes())
    assert isinstance(written, dict)
    assert written["differences"] == []


def test_mutated_duration_yields_single_diff() -> None:
    """Given: two extractions that differ only in duration.

    When: diffed.
    Then: exactly one difference row naming the mutated field.
    """

    # Given: a fresh extraction from the fixture
    ir = _load_fixture_ir()
    # Use the harness's legacy builder indirectly via run_parity's structure,
    # but also exercise extract_structure directly for adversarial probe.

    lock_path = Path("config/toolchains/phase-2-v2.json")
    lock = load_lock(lock_path)
    assert isinstance(lock, Phase2ToolchainLock)
    lock_sha = sha256_file(lock_path)
    # Deterministic bindings matching harness logic
    source_ids = sorted(
        {
            item.source.source_id  # type: ignore[union-attr]
            for track in ir.tracks
            for item in track.items
            if hasattr(item, "source")
        }
    )
    max_end: dict[str, int] = {}
    for track in ir.tracks:
        for item in track.items:
            if not hasattr(item, "source"):  # type: ignore[union-attr]
                continue
            sid: str = item.source.source_id  # type: ignore[union-attr]
            end: int = item.source.span.end_frame  # type: ignore[union-attr]
            max_end[sid] = max(max_end.get(sid, 0), end)
    bindings = tuple(
        MediaBinding(
            source_id=sid,
            path=f"jobs/parity/sources/{sid}.mov",
            sha256=hashlib.sha256(sid.encode()).hexdigest(),
            duration_frames=max_end[sid],
        )
        for sid in source_ids
    )
    package = compile_resolve_package(
        PackageCompileRequest(
            ir=ir,
            lock=lock,
            lock_sha256=lock_sha,
            declared_media=bindings,
            artifact_id=f"resolve-package-parity-{ir.artifact_id}",
        )
    )
    baseline = extract_structure(package)

    # When: mutate duration by exactly one frame
    mutated = dict(baseline)
    mutated["duration"] = int(baseline["duration"]) + 1  # type: ignore[arg-type]
    differences = diff_structures(baseline, mutated)

    # Then: exactly one row naming duration
    assert len(differences) == 1
    assert differences[0]["field"] == "duration"
    assert differences[0]["a"] == baseline["duration"]
    assert differences[0]["b"] == mutated["duration"]


def test_fixture_round_trip_matches_expected_extract() -> None:
    """Given: ir.json on disk.

    When: loading the IR and extracting via the legacy package.
    Then: the extraction matches expected_extract.json (stale_state probe).
    """

    # Given: the canonical IR file and the recorded expected extraction
    assert IR_PATH.is_file()
    assert EXPECTED_PATH.is_file()
    ir = _load_fixture_ir()
    expected = _expected_extract()

    # When: fresh extraction via the harness's legacy path
    fresh = _build_legacy_structure(ir)

    # Then: byte-stable match (sort_keys) — stale expected file would fail
    assert json.dumps(fresh, sort_keys=True, separators=(",", ":")) == json.dumps(
        expected, sort_keys=True, separators=(",", ":")
    )
    # Also verify extraction has exactly the six required keys
    assert set(fresh.keys()) == {
        "source_ids",
        "source_in_out",
        "record_positions",
        "track_mapping",
        "duration",
        "render_properties",
    }
