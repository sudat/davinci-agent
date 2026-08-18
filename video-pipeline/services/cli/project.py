"""Project the committed production plan onto the frozen review plane.

The review plane reuses the frozen Phase-0C contracts (``EditPlan0C``, the
Todo-30 store, the Todo-27 preview adapter): video items carry the SEGMENT id
(the review vocabulary of the declared correction sequence), audio items are
``a{position}``, subtitle items keep the manifest's declared subtitle ids, and
A/V link groups are ``av{position}``. The projection is a pure function of the
committed EditPlan (Todo 43) plus the fixture manifest's declared subtitle
table, so the initial review plan reproduces the manifest's expected plan
items exactly.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from services.compile.phase0c import build_ir
from services.contracts.edit_plan_0c import (
    EditPlan0C,
    EditPlanBody0C,
    EditPlanItem0C,
    EditSourceRef0C,
)
from services.contracts.primitives import Producer, RationalFrameRate, SourceFrameSpan
from services.foundation_io import canonical_model_bytes
from services.review_command.store import initialize_store

if TYPE_CHECKING:
    from services.contracts.timeline_ir import TimelineIr0C
    from services.editorial.reconcile import ReconciliationResult
    from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest
    from services.plan.edit_plan_models import EditPlan

REVIEW_PRODUCER = Producer(name="phase1-review-plane", version="1")
VIDEO_TRACK: int = 1
AUDIO_TRACK: int = 2
SUBTITLE_TRACK: int = 3


class ProjectionError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ReviewPlane:
    plan: EditPlan0C
    segment_of_item: dict[str, str]


def project_review_plan(
    committed: EditPlan,
    reconciled: ReconciliationResult,
    manifest: Phase1TechnicalFixtureManifest,
) -> ReviewPlane:
    """Deterministic projection: committed EditPlan -> review-plane EditPlan0C."""

    rate = RationalFrameRate(
        num=manifest.edit_source.frame_rate_num, den=manifest.edit_source.frame_rate_den
    )
    candidate_segment = {
        candidate_id: link.segment_id
        for link in reconciled.segment_links
        for candidate_id in link.candidate_ids
    }
    items: list[EditPlanItem0C] = []
    segment_of_item: dict[str, str] = {}
    position = 0
    for item in committed.items:
        if item.track_kind != "video":
            continue
        position += 1
        segment_id = candidate_segment.get(item.provenance.candidate_id)
        if segment_id is None:
            raise ProjectionError(
                "unlinked_segment",
                f"committed video item {item.item_id[:12]} has no selection segment",
            )
        audio = next(
            (
                other
                for other in committed.items
                if other.track_kind == "audio" and other.link_group_id == item.item_id
            ),
            None,
        )
        if audio is None:
            raise ProjectionError(
                "av_link_broken", f"video item {segment_id} has no audio counterpart"
            )
        link = f"av{position}"
        items.append(
            EditPlanItem0C(
                item_id=segment_id,
                kind="video",
                source_id=manifest.edit_source.source_id,
                span=SourceFrameSpan(
                    start_frame=item.span.start_frame,
                    end_frame=item.span.end_frame,
                    rate=rate,
                ),
                track_index=VIDEO_TRACK,
                av_link_id=link,
            )
        )
        items.append(
            EditPlanItem0C(
                item_id=f"a{position}",
                kind="audio",
                source_id=manifest.edit_source.source_id,
                span=SourceFrameSpan(
                    start_frame=audio.span.start_frame,
                    end_frame=audio.span.end_frame,
                    rate=rate,
                ),
                track_index=AUDIO_TRACK,
                av_link_id=link,
            )
        )
        segment_of_item[segment_id] = segment_id
        segment_of_item[f"a{position}"] = segment_id
    kept_segments = {item.item_id for item in items if item.kind == "video"}
    for subtitle in manifest.subtitles:
        if subtitle.segment_id not in kept_segments:
            continue
        items.append(
            EditPlanItem0C(
                item_id=subtitle.subtitle_id,
                kind="subtitle",
                source_id=manifest.edit_source.source_id,
                span=SourceFrameSpan(
                    start_frame=subtitle.span.start_frame,
                    end_frame=subtitle.span.end_frame,
                    rate=rate,
                ),
                track_index=SUBTITLE_TRACK,
                subtitle_text=subtitle.text,
            )
        )
        segment_of_item[subtitle.subtitle_id] = subtitle.segment_id
    if position == 0:
        raise ProjectionError("empty_projection", "the committed plan keeps no video items")
    plan = EditPlan0C(
        artifact_id=f"edit-plan-review-{manifest.fixture_id}",
        artifact_type="edit_plan_0c",
        schema_version="edit-plan-0c-v1",
        content_hash="0" * 64,
        producer=REVIEW_PRODUCER,
        inputs=(),
        frame_rate=rate,
        plan=EditPlanBody0C(
            plan_version="v1",
            edit_source=EditSourceRef0C(
                source_id=manifest.edit_source.source_id,
                total_frames=manifest.edit_source.total_frames,
            ),
            items=tuple(items),
        ),
    )
    return ReviewPlane(plan=plan, segment_of_item=segment_of_item)


def review_ir(plan: EditPlan0C) -> TimelineIr0C:
    return build_ir(plan, f"{plan.artifact_id}-ir", ())


__all__ = [
    "ProjectionError",
    "ReviewPlane",
    "init_review_store",
    "plan_sha256",
    "project_review_plan",
    "review_ir",
]


def plan_sha256(plan: EditPlan0C) -> str:
    return hashlib.sha256(canonical_model_bytes(plan)).hexdigest()


def init_review_store(plan: EditPlan0C, store_dir: Path) -> Path:
    log = store_dir / "events.jsonl"
    initialize_store(plan, log, store_dir)
    return log
