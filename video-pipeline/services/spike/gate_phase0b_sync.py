"""Subtitle/audio anchor and A/V sync measurements over committed ConformMaps.

Every row is MEASURED through the frozen conform machinery (map rows, the
audio affine map, the frozen tick assignment) — never copied from an authored
``passed`` flag. Exact rows (``exact-zero``) assert golden integer equality;
the ``tolerance-one-frame`` rows carry the perceptual subtitle↔audio and
video↔audio comparisons whose absolute delta must stay within one timeline
frame at the CFR30 timeline rate.
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING, Final, Literal

from services.conform.convert import frame_conversion_accounting, round_half_away
from services.conform.errors import CoordinateError
from services.conform.map_query import original_sample_to_edit_sample
from services.contracts.primitives import RationalFrameRate, SourceFrameSpan
from services.spike.gate_phase0b_models import (
    SAMPLES_PER_SECOND,
    TIMELINE_RATE_DEN,
    TIMELINE_RATE_NUM,
    RationalValue,
    SyncRow,
)

if TYPE_CHECKING:
    from services.conform.map_models import ConformMap
    from services.fixtures.manifest_phase0b import Phase0BFixtureManifest

RowKind = Literal["exact-zero", "tolerance-one-frame"]
EXACT: Final[RowKind] = "exact-zero"
TOLERANCE: Final[RowKind] = "tolerance-one-frame"
HUGE_NUM: Final = 10**9
ONE_FRAME: Final = Fraction(1)


def edit_frame_for_source_frame(conform_map: ConformMap, source_frame: int) -> int:
    """Later-wins first edit frame showing ``source_frame`` (map row query)."""

    for row in conform_map.video_table.rows:
        if row.source_frame >= source_frame:
            return row.edit_frame
    return conform_map.video_table.output_frames


def _rate(manifest: Phase0BFixtureManifest) -> RationalFrameRate:
    table = manifest.rational_frame_rate_table
    return RationalFrameRate(num=table.frame_rate.num, den=table.frame_rate.den)


def source_span(manifest: Phase0BFixtureManifest) -> SourceFrameSpan:
    frames = manifest.conversions["cfr30"].input_frames
    return SourceFrameSpan(start_frame=0, end_frame=frames, rate=_rate(manifest))


def deterministic_tick(
    manifest: Phase0BFixtureManifest, source_frame: int, target_num: int, target_den: int
) -> int:
    """Golden tick conversion for one source frame (frozen assignment rule)."""

    rate = _rate(manifest).as_fraction
    seconds = Fraction(source_frame) / rate
    return round_half_away(seconds * Fraction(target_num, target_den))


def _row(
    variant: str,
    name: str,
    kind: RowKind,
    expected: int,
    observed: int,
    delta: Fraction,
) -> SyncRow:
    limit = ONE_FRAME if kind == TOLERANCE else Fraction(0)
    return SyncRow(
        variant=variant,
        name=name,
        kind=kind,
        expected=expected,
        observed=observed,
        delta=RationalValue(num=delta.numerator, den=delta.denominator),
        passed=abs(delta) <= limit,
    )


def _exact_row(variant: str, name: str, expected: int, observed: int) -> SyncRow:
    return _row(variant, name, EXACT, expected, observed, Fraction(observed - expected))


def _failed(variant: str, name: str, expected: int) -> SyncRow:
    return _row(variant, name, EXACT, expected, -1, Fraction(HUGE_NUM))


def _audio_clock_frames(edit_sample: int) -> Fraction:
    return Fraction(edit_sample * TIMELINE_RATE_NUM, TIMELINE_RATE_DEN * SAMPLES_PER_SECOND)


def _cue_boundary_tick(conform_map: ConformMap, boundary: int) -> int:
    return edit_frame_for_source_frame(conform_map, boundary)


def _edit_sample(conform_map: ConformMap, sample: int) -> int:
    try:
        return original_sample_to_edit_sample(conform_map, sample)
    except CoordinateError:
        return -1


def measure_variant(
    variant: str, manifest: Phase0BFixtureManifest, conform_map: ConformMap
) -> tuple[SyncRow, ...]:
    """Measure every subtitle/audio anchor and A/V sync row for one variant."""

    rows: list[SyncRow] = []
    dropped30 = set(conform_map.normalization.dropped_source_frames)
    span = source_span(manifest)
    accounting24 = frame_conversion_accounting(
        span,
        RationalFrameRate(num=24, den=1),
    )
    dropped24 = set(accounting24.dropped_source_frames)
    rate = _rate(manifest).as_fraction

    for cue in manifest.subtitle_anchors:
        start, end = cue.source_frame_span.start_frame, cue.source_frame_span.end_frame
        start_tick = _cue_boundary_tick(conform_map, start)
        end_tick = _cue_boundary_tick(conform_map, end)
        rows.append(
            _row(
                variant,
                f"subtitle:{cue.cue_id}:cfr30-start",
                EXACT,
                cue.cfr30_tick_span.start_frame,
                start_tick,
                Fraction(start_tick - cue.cfr30_tick_span.start_frame),
            )
        )
        rows.append(
            _row(
                variant,
                f"subtitle:{cue.cue_id}:cfr30-end",
                EXACT,
                cue.cfr30_tick_span.end_frame,
                end_tick,
                Fraction(end_tick - cue.cfr30_tick_span.end_frame),
            )
        )
        for boundary, tick in (
            (start, cue.cfr24_tick_span.start_frame),
            (end, cue.cfr24_tick_span.end_frame),
        ):
            measured24 = deterministic_tick(manifest, boundary, 24, 1)
            rows.append(
                _row(
                    variant,
                    f"subtitle:{cue.cue_id}:cfr24:{boundary}",
                    EXACT,
                    tick,
                    measured24,
                    Fraction(measured24 - tick),
                )
            )
            edit_sample = _edit_sample(
                conform_map, round_half_away(Fraction(boundary) / rate * SAMPLES_PER_SECOND)
            )
            audio_clock = (
                _audio_clock_frames(edit_sample) if edit_sample >= 0 else Fraction(HUGE_NUM)
            )
            boundary_tick = _cue_boundary_tick(conform_map, boundary)
            rows.append(
                _row(
                    variant,
                    f"subtitle-audio:{cue.cue_id}:{boundary}",
                    TOLERANCE,
                    boundary_tick,
                    boundary_tick,
                    Fraction(boundary_tick) - audio_clock,
                )
            )

    for anchor in manifest.audio_sample_anchors:
        tick30 = edit_frame_for_source_frame(conform_map, anchor.source_frame)
        if anchor.cfr30_tick is None:
            if anchor.source_frame not in dropped30:
                rows.append(_failed(variant, f"audio:{anchor.pulse_id}:cfr30-dropped", 1))
        else:
            rows.append(
                _row(
                    variant,
                    f"audio:{anchor.pulse_id}:cfr30-tick",
                    EXACT,
                    anchor.cfr30_tick,
                    tick30,
                    Fraction(tick30 - anchor.cfr30_tick),
                )
            )
        if anchor.cfr24_tick is None:
            if anchor.source_frame not in dropped24:
                rows.append(_failed(variant, f"audio:{anchor.pulse_id}:cfr24-dropped", 1))
        else:
            measured24 = deterministic_tick(manifest, anchor.source_frame, 24, 1)
            rows.append(
                _exact_row(
                    variant, f"audio:{anchor.pulse_id}:cfr24-tick", anchor.cfr24_tick, measured24
                )
            )
        edit_sample = _edit_sample(conform_map, anchor.source_sample)
        if edit_sample < 0:
            rows.append(
                _failed(variant, f"audio:{anchor.pulse_id}:edit-sample", anchor.cfr30_sample)
            )
        else:
            rows.append(
                _exact_row(
                    variant, f"audio:{anchor.pulse_id}:edit-sample", anchor.cfr30_sample,
                    edit_sample,
                )
            )

    for marker in manifest.resolve_readback:
        expected = marker.cfr30_frame if marker.cfr30_frame is not None else -1
        measured = edit_frame_for_source_frame(conform_map, marker.source_frame)
        rows.append(
            _exact_row(variant, f"marker:{marker.marker_id}:cfr30-frame", expected, measured)
        )
        edit_sample = _edit_sample(conform_map, marker.audio_sample)
        audio_clock = _audio_clock_frames(edit_sample) if edit_sample >= 0 else Fraction(HUGE_NUM)
        rows.append(
            _row(
                variant,
                f"av-sync:{marker.marker_id}",
                TOLERANCE,
                measured,
                measured,
                Fraction(measured) - audio_clock,
            )
        )
    return tuple(rows)
