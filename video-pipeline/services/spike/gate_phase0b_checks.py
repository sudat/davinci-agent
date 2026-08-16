"""Per-variant criterion checks for the Phase-0B gate evaluator.

Pure recomputation helpers: golden/model/manifest/record/map agreement,
drop/duplicate coverage from the explicit map rows, live-readback re-derivation
against the frozen manifest anchors, and sync-row collection. Each helper only
appends structured mismatches; the orchestrator decides pass/stop.
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING, Final, Literal

from services.conform.convert import frame_conversion_accounting
from services.conform.map_validate import MapValidationError, validate_conform_map
from services.contracts.primitives import RationalFrameRate
from services.normalize.models import verify_normalize_record_hash
from services.spike.gate_phase0b_models import (
    CRITERION_DROPS,
    CRITERION_GOLDEN,
)
from services.spike.gate_phase0b_sync import source_span

if TYPE_CHECKING:
    from services.conform.map_models import ConformMap
    from services.fixtures.manifest_phase0b import Phase0BFixtureManifest
    from services.ingest.models import SourceManifest
    from services.normalize.models import NormalizeRecord

CODE_OFF_BY_ONE: Final = "off-by-one-golden"
CODE_MISSING_DUP: Final = "missing-duplicate-report"
CODE_SYNC_OVER: Final = "sync-over-one-frame"
CODE_STALE: Final = "stale-prior-build-evidence"


class CheckState:
    """Accumulates criterion booleans and structured mismatches."""

    def __init__(self, criteria: tuple[str, ...]) -> None:
        self.criteria: dict[str, bool] = dict.fromkeys(criteria, True)
        self.rows: list[tuple[str, str]] = []
        self.evidence_sha: dict[str, list[str]] = {criterion: [] for criterion in criteria}

    def fail(self, criterion: str, code: str, detail: str) -> None:
        self.criteria[criterion] = False
        self.rows.append((code, detail))

    def note(self, criterion: str, sha: str) -> None:
        if sha and sha not in self.evidence_sha[criterion]:
            self.evidence_sha[criterion].append(sha)


def _table(payload: dict[str, object], variant: str, target: str) -> dict[str, object] | None:
    row = payload.get(variant)
    table = row.get(target) if isinstance(row, dict) else None
    return table if isinstance(table, dict) else None


def _golden_triple(table: dict[str, object]) -> tuple[int, list[int], list[int]] | None:
    output = table.get("output_frames")
    dropped = table.get("dropped_source_frames")
    duplicated = table.get("duplicated_source_frames")
    if not isinstance(output, int) or not isinstance(dropped, list | tuple):
        return None
    if not isinstance(duplicated, list | tuple):
        return None
    return int(output), [int(item) for item in dropped], [int(item) for item in duplicated]


def _code_for(
    observed: tuple[int, list[int], list[int]], want: tuple[int, list[int], list[int]]
) -> str:
    if abs(observed[0] - want[0]) == 1 and observed[1:] == want[1:]:
        return CODE_OFF_BY_ONE
    return "golden-tables-mismatch"


def check_golden(
    variant: str,
    fixture: Phase0BFixtureManifest,
    golden: dict[str, object],
    manifest: SourceManifest | None,
    record: NormalizeRecord | None,
    conform_map: ConformMap | None,
    state: CheckState,
) -> None:
    span = source_span(fixture)
    table_rate = fixture.rational_frame_rate_table.frame_rate
    rate = RationalFrameRate(num=table_rate.num, den=table_rate.den)
    if (rate.num, rate.den) != (span.rate.num, span.rate.den) or (
        span.end_frame != fixture.conversions["cfr30"].input_frames
    ):
        state.fail(CRITERION_GOLDEN, "manifest-table-inconsistent", f"{variant}: span mismatch")
        return
    targets: tuple[tuple[Literal["cfr24", "cfr30"], RationalFrameRate], ...] = (
        ("cfr24", RationalFrameRate(num=24, den=1)),
        ("cfr30", RationalFrameRate(num=30, den=1)),
    )
    for target, target_rate in targets:
        computed = frame_conversion_accounting(span, target_rate)
        model_side = (
            computed.output_frames,
            list(computed.dropped_source_frames),
            list(computed.duplicated_source_frames),
        )
        frozen = fixture.conversions[target]
        manifest_side = (
            frozen.output_frames,
            list(frozen.dropped_source_frames),
            list(frozen.duplicated_source_frames),
        )
        if model_side != manifest_side:
            state.fail(
                CRITERION_GOLDEN,
                _code_for(model_side, manifest_side),
                f"{variant}:{target} conform model != manifest",
            )
        golden_table = _table(golden, variant, target)
        golden_side = _golden_triple(golden_table) if golden_table is not None else None
        if golden_side is None:
            state.fail(CRITERION_GOLDEN, "golden-table-missing", f"{variant}:{target}")
        elif model_side != golden_side:
            state.fail(
                CRITERION_GOLDEN,
                _code_for(model_side, golden_side),
                f"{variant}:{target} conform model != golden",
            )
    if manifest is None or record is None:
        return
    video = next(
        (s for s in manifest.streams if getattr(s, "codec_type", "") == "video"), None
    )
    table = fixture.rational_frame_rate_table
    if video is None or Fraction(video.duration_num, video.duration_den) != Fraction(
        table.duration_seconds.num, table.duration_seconds.den
    ):
        state.fail(
            CRITERION_GOLDEN,
            "evidence-manifest-duration-drift",
            f"{variant}: ingest stream duration != frozen rational table",
        )
    conv30 = fixture.conversions["cfr30"]
    golden30 = (
        conv30.output_frames,
        list(conv30.dropped_source_frames),
        list(conv30.duplicated_source_frames),
    )
    record_side = (
        record.drop_dup.expected.output_frames,
        list(record.drop_dup.expected.dropped),
        list(record.drop_dup.expected.duplicated),
    )
    if record_side != golden30:
        state.fail(
            CRITERION_GOLDEN,
            _code_for(record_side, golden30),
            f"{variant}: normalize record != golden cfr30",
        )
    if record.output_semantics.observed_output_frames != conv30.output_frames:
        state.fail(
            CRITERION_GOLDEN,
            CODE_OFF_BY_ONE
            if abs(record.output_semantics.observed_output_frames - conv30.output_frames) == 1
            else "golden-tables-mismatch",
            f"{variant}: observed output frames != golden cfr30",
        )
    if not verify_normalize_record_hash(record):
        state.fail(CRITERION_GOLDEN, "record-hash-invalid", variant)
    if conform_map is None:
        return
    try:
        validate_conform_map(conform_map, source_manifest=manifest, normalize_record=record)
    except MapValidationError as error:
        state.fail(
            CRITERION_GOLDEN,
            "map-validation-failed",
            f"{variant}: {error.reason_code}: {error.detail}",
        )
    map_side = (
        conform_map.normalization.output_frames,
        list(conform_map.normalization.dropped_source_frames),
        list(conform_map.normalization.duplicated_source_frames),
    )
    if map_side != golden30:
        state.fail(CRITERION_GOLDEN, "map-golden-drift", f"{variant}")
    if map_side != record_side:
        state.fail(CRITERION_DROPS, CODE_MISSING_DUP, f"{variant}: map != record accounting")


def check_drop_coverage(variant: str, conform_map: ConformMap, state: CheckState) -> None:
    table = conform_map.video_table
    rows = table.rows
    shown = {row.source_frame for row in rows}
    dropped = set(conform_map.normalization.dropped_source_frames)
    if shown & dropped or (shown | dropped) != set(range(table.source_frame_count)):
        state.fail(
            CRITERION_DROPS,
            "coverage-gap",
            f"{variant}: shown/dropped do not partition the decoded span",
        )
    flagged = {
        rows[index].source_frame
        for index in range(1, len(rows))
        if rows[index].source_frame == rows[index - 1].source_frame
    }
    if flagged != set(conform_map.normalization.duplicated_source_frames):
        state.fail(
            CRITERION_DROPS,
            CODE_MISSING_DUP,
            f"{variant}: recomputed duplicate flags != reported duplicates",
        )

