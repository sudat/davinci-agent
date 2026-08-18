"""Candidate-table parity recomputation vs the goldens (Phase-1 gate).

Recomputes every golden candidate row — decision, weighted score, forced
flag, and the drop/keep reason — from the declared manifest inputs plus the
selected segment set, and compares against the independently derived golden
table one row at a time.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from services.job_runner.gate_p1_checks import (
    C_E2E,
    CheckState,
    MalformedEvidenceError,
    mapping_of,
    str_list,
)

if TYPE_CHECKING:
    from services.fixtures.manifest_phase1 import (
        Phase1TechnicalFixtureManifest,
        TranscriptSegment,
)


def check_candidate_table(  # noqa: PLR0913, PLR0917 (golden parity inputs)
    segments: Mapping[str, TranscriptSegment],
    manifest: Phase1TechnicalFixtureManifest,
    golden: Mapping[str, object],
    selected: list[str],
    state: CheckState,
    fixture: str,
) -> None:
    scoring = manifest.editorial_rules.scoring
    must = set(manifest.editorial_rules.must_include.segment_ids)
    budget_dropped = set(str_list(mapping_of(golden["selection"])["budget_dropped"]))
    table = golden["candidate_table"]
    if not isinstance(table, list):
        raise MalformedEvidenceError("candidate_table is not an array")
    golden_rows = {str(mapping_of(row)["segment_id"]): mapping_of(row) for row in table}
    if set(golden_rows) != set(segments):
        state.fail(C_E2E, "candidate-table-mismatch", f"{fixture}: golden table ids diverge")
        return
    for segment_id, segment in segments.items():
        score = (
            segment.observed.content_score * scoring.content_weight
            + segment.observed.clarity_score * scoring.clarity_weight
        )
        decision = "selected" if segment_id in selected else "dropped"
        forced = segment_id in must
        length = segment.span.end_frame - segment.span.start_frame
        reason = expected_reason(
            segment.kind,
            decision,
            forced,
            score,
            length,
            segment_id,
            manifest,
            budget_dropped,
            selected,
            segments,
        )
        row = golden_rows[segment_id]
        if (row["decision"], row["score"], row["forced"], row["reason_code"]) != (
            decision,
            score,
            forced,
            reason,
        ):
            state.fail(
                C_E2E,
                "candidate-table-mismatch",
                f"{fixture}/{segment_id}: golden {dict(row)} != recomputed "
                f"({decision},{score},{forced},{reason})",
            )
            return


def expected_reason(  # noqa: PLR0913, PLR0917, PLR0911 (golden reason table)
    kind: str,
    decision: str,
    forced: bool,  # noqa: FBT001 (golden `forced` column is a plain boolean)
    score: int,
    length: int,
    segment_id: str,
    manifest: Phase1TechnicalFixtureManifest,
    budget_dropped: set[str],
    selected: list[str],
    segments: Mapping[str, TranscriptSegment],
) -> str:
    rules = manifest.editorial_rules
    if forced:
        return "must-include-forced"
    if decision == "selected":
        return "short-pause-retained" if kind == "pause" else "score-selected"
    if kind == "filler":
        return "filler"
    if kind == "false_start":
        return "false-start"
    if kind == "pause":
        return (
            "long-pause-deleted"
            if length >= rules.pauses.delete_threshold_frames
            else "short-pause-retained"
        )
    if score < rules.scoring.min_speech_score:
        return "below-min-score"
    if segment_id in budget_dropped:
        return "budget-dropped"
    group = segments[segment_id].observed.retake_group
    if group is not None and any(
        other.observed.retake_group == group and other.segment_id in selected
        for other in segments.values()
    ):
        return "retake-superseded"
    return "unexplained-drop"




__all__ = ["check_candidate_table", "expected_reason"]
