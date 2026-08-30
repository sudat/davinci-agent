"""Typed readback verification + Build-Report row mapping (task 39).

Each step's ``expected_readback`` (one of the 11 T38 kinds) is verified
STRUCTURALLY against the actual payload the executor returned: field by
field, mismatch = step failure (never an exception, never a silent
pass). The actual is data — a mapping of reported values; nothing here
calls a server or re-derives timeline state. Span verification compares
frame bounds; the span's plan-side rate is metadata the executor does
not report.

:meth:`to_build_report_rows` maps a run report into the existing
resolve-bridge Build Report convention (``BuildWarning0A`` /
``BuildFailure0A`` rows carry exactly ``{code, detail}``) — an ADDITIVE
section a Build Report writer can merge in.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, assert_never

from pydantic import BaseModel, BeforeValidator, Field

from services.contracts.primitives import RecordFrameSpan, SourceFrameSpan, StrictModel
from services.mcp_execution.plan_payloads import (
    AudioMetricReadback,
    AudioStateReadback,
    CueCountReadback,
    ExpectedReadback,
    GradeReadback,
    ImportReadback,
    ManualNoteReadback,
    PlacementReadback,
    ProjectReadback,
    RecipeParamsReadback,
    RenderNativeReadback,
    SetTransformReadback,
    SubtitleCuePayload,
    SubtitleCuesReadback,
    TitleReadback,
    TransformReadback,
)

if TYPE_CHECKING:
    from services.mcp_execution.runner import McpExecutionRunReportV1


class ReadbackVerification(StrictModel):
    """Structural comparison outcome for one expected readback."""

    kind: str = Field(min_length=1, strict=True)
    matched: bool
    mismatches: Annotated[
        tuple[str, ...],
        BeforeValidator(lambda value: tuple(value) if isinstance(value, list) else value),
    ] = ()


def _as_mapping(actual: object) -> Mapping[str, object] | None:
    if isinstance(actual, Mapping):
        return actual
    if isinstance(actual, BaseModel):
        return actual.model_dump(mode="json")
    return None


def _compare(actual: Mapping[str, object], values: Mapping[str, object]) -> tuple[str, ...]:
    mismatches: list[str] = []
    for field, expected in values.items():
        if field not in actual:
            mismatches.append(f"{field}: missing")
        elif actual[field] != expected:
            mismatches.append(f"{field}: expected {expected!r}, got {actual[field]!r}")
    return tuple(mismatches)


def _compare_spans(
    actual: Mapping[str, object], spans: Mapping[str, tuple[int, int]]
) -> tuple[str, ...]:
    mismatches: list[str] = []
    for field, expected in spans.items():
        got = actual.get(field)
        if not isinstance(got, Mapping):
            suffix = "missing" if got is None else f"not a span object, got {got!r}"
            mismatches.append(f"{field}: {suffix}")
            continue
        try:
            got_pair = (got["start_frame"], got["end_frame"])
        except KeyError:
            mismatches.append(f"{field}: span object missing start_frame/end_frame")
            continue
        if got_pair != expected:
            mismatches.append(f"{field}: expected {expected}, got {got_pair}")
    return tuple(mismatches)


def _compare_names(
    actual: Mapping[str, object], names: Mapping[str, tuple[str, ...]]
) -> tuple[str, ...]:
    mismatches: list[str] = []
    for field, expected in names.items():
        got = actual.get(field)
        if not isinstance(got, list | tuple):
            suffix = "missing" if got is None else f"not a list, got {got!r}"
            mismatches.append(f"{field}: {suffix}")
            continue
        got_tuple = tuple(got)
        if got_tuple != expected:
            mismatches.append(f"{field}: expected {expected!r}, got {got_tuple!r}")
    return tuple(mismatches)


def _compare_cues(
    actual: Mapping[str, object], cues: tuple[SubtitleCuePayload, ...]
) -> tuple[str, ...]:
    """The committed cue set verified per cue: count, id, text, span.

    Style evidence is handler-sourced (verified against the style binding
    at mutation time) and is deliberately not compared here — only the
    committed fields gate at the runner level.
    """

    got = actual.get("cues")
    if not isinstance(got, list | tuple):
        suffix = "missing" if got is None else f"not a list, got {got!r}"
        return (f"cues: {suffix}",)
    if len(got) != len(cues):
        return (f"cues: expected {len(cues)} cues, got {len(got)}",)
    mismatches: list[str] = []
    for row, cue in zip(got, cues, strict=True):
        if not isinstance(row, Mapping):
            mismatches.append(f"cue {cue.cue_id}: not an object, got {row!r}")
            continue
        if row.get("cue_id") != cue.cue_id:
            mismatches.append(
                f"cue {cue.cue_id}: cue_id expected {cue.cue_id!r}, got {row.get('cue_id')!r}"
            )
        if row.get("text") != cue.text:
            mismatches.append(
                f"cue {cue.cue_id}: text expected {cue.text!r}, got {row.get('text')!r}"
            )
        span = row.get("record_span")
        if isinstance(span, Mapping):
            pair = (span.get("start_frame"), span.get("end_frame"))
            expected_pair = (cue.record_span.start_frame, cue.record_span.end_frame)
            if pair != expected_pair:
                mismatches.append(f"cue {cue.cue_id}: record_span expected {expected_pair}, got {pair}")  # noqa: E501
        else:
            suffix = "missing" if span is None else f"not a span object, got {span!r}"
            mismatches.append(f"cue {cue.cue_id}: record_span {suffix}")
    return tuple(mismatches)


def _bounds(expected: AudioMetricReadback, actual: Mapping[str, object]) -> tuple[str, ...]:
    value = actual.get("value")
    if not isinstance(value, int | float) or isinstance(value, bool):
        return (f"value: expected a number, got {value!r}",)
    if not expected.minimum <= value <= expected.maximum:
        return (f"value: expected within [{expected.minimum}, {expected.maximum}], got {value}",)
    return ()


def _frames(span: SourceFrameSpan | RecordFrameSpan) -> tuple[int, int]:
    return (span.start_frame, span.end_frame)


def verify_readback(  # noqa: C901, PLR0912 (flat 12-kind match; splitting would separate kinds)
    expected: ExpectedReadback, actual: object
) -> ReadbackVerification:
    """Verify the executor's actual payload against the expected readback."""
    actual_map = _as_mapping(actual)
    if actual_map is None:
        return ReadbackVerification(
            kind=expected.kind, matched=False, mismatches=("actual: not an object",)
        )
    mismatches: tuple[str, ...]
    match expected:
        case ProjectReadback():
            mismatches = _compare(
                actual_map,
                {
                    "project_name": expected.project_name,
                    "timeline_frame_rate": expected.timeline_frame_rate,
                },
            )
        case ImportReadback():
            mismatches = _compare(actual_map, {"source_id": expected.source_id})
        case PlacementReadback():
            mismatches = _compare(actual_map, {"item_id": expected.item_id}) + _compare_spans(
                actual_map,
                {
                    "source_span": _frames(expected.source_span),
                    "record_span": _frames(expected.record_span),
                },
            )
        case TitleReadback():
            mismatches = _compare(actual_map, {"item_id": expected.item_id}) + _compare_spans(
                actual_map, {"record_span": _frames(expected.record_span)}
            )
        case CueCountReadback():
            mismatches = _compare(actual_map, {"cue_count": expected.cue_count})
        case SubtitleCuesReadback():
            mismatches = _compare_cues(actual_map, expected.cues)
        case AudioStateReadback():
            mismatches = _compare(
                actual_map,
                {"item_ref": expected.item_ref, "state_property": expected.state_property},
            )
        case AudioMetricReadback():
            mismatches = _compare(
                actual_map,
                {"stage": expected.stage, "metric": expected.metric, "unit": expected.unit},
            ) + _bounds(expected, actual_map)
        case GradeReadback():
            mismatches = _compare(
                actual_map, {"target": expected.target, "preset_ref": expected.preset_ref}
            )
        case TransformReadback():
            mismatches = _compare(actual_map, {"item_id": expected.item_id}) + _compare_names(
                actual_map, {"properties": tuple(expected.properties)}
            )
        case ManualNoteReadback():
            mismatches = _compare(actual_map, {"expectation": expected.expectation})
        case RecipeParamsReadback():
            mismatches = _compare(actual_map, {"recipe_id": expected.recipe_id}) + _compare_names(
                actual_map, {"param_names": tuple(expected.param_names)}
            )
        case RenderNativeReadback():
            mismatches = _compare(actual_map, {"custom_name": expected.custom_name})
        case SetTransformReadback():
            mismatches = _compare(
                actual_map,
                {
                    "track_index": expected.track_index,
                    "item_index": expected.item_index,
                    "rotation_angle": expected.rotation_angle,
                },
            )
        case unreachable:
            assert_never(unreachable)
    return ReadbackVerification(kind=expected.kind, matched=not mismatches, mismatches=mismatches)


# ------------------------------------------- Build Report row mapping


def to_build_report_rows(
    report: McpExecutionRunReportV1,
) -> dict[str, tuple[dict[str, str], ...]]:
    """Map a run report into Build-Report-compatible ``{code, detail}`` rows.

    Mirrors the resolve-bridge Build Report convention (warnings carry
    rung changes and retries; failures carry failed steps). Additive: the
    caller merges these rows into its own report artifact.
    """
    warnings = [
        {
            "code": f"mcp-fallback-{entry.to_rung}",
            "detail": (
                f"{entry.step}: {entry.from_rung} -> {entry.to_rung} "
                f"({entry.source}); {entry.reason}"
            ),
        }
        for entry in report.rung_entries
    ]
    failures: list[dict[str, str]] = []
    for step in report.steps:
        if step.status == "completed":
            if len(step.attempts) > 1:
                warnings.append(
                    {
                        "code": f"mcp-retry-{step.step_id}",
                        "detail": (
                            f"{len(step.attempts)} attempts; first failure: "
                            f"{step.attempts[0].detail}"
                        ),
                    }
                )
            continue
        failures.append(
            {
                "code": f"mcp-step-failed-{step.step_id}",
                "detail": f"{step.failure_code}; {step.detail}"
                if step.detail
                else str(step.failure_code),
            }
        )
    return {"warnings": tuple(warnings), "failures": tuple(failures)}


__all__ = [
    "ReadbackVerification",
    "to_build_report_rows",
    "verify_readback",
]
