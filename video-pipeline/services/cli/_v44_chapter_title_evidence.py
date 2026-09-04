"""Read-only verification of the approved chapter boundary and subtitle evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from services.cli._v44_chapter_title_contracts import (
    ChapterBoundaryBinding,
    SubtitleEvidenceCue,
    SubtitleEvidenceSet,
)
from services.cli._v44_finishing_build import FinishingError, resolve_review_store
from services.episode_cockpit.models import IntakeRecordV1
from services.review_command.store import HeadState, ReviewCommitError, load_head

if TYPE_CHECKING:
    from services.contracts.edit_plan_0c import EditPlan0C

FRAME_RATE_NUM: Final = 30
FRAME_RATE_DEN: Final = 1


class ChapterTitleProposalError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ApprovedChapterBoundary:
    episode_id: str
    plan_version: int
    plan_sha256: str
    left_item_id: str
    right_item_id: str
    record_frame: int
    source_frame: int
    item_count_per_kind: int
    total_record_frames: int
    first_subtitle_id: str
    evidence_cue_count: int


APPROVED_CHAPTER_BOUNDARY: Final = ApprovedChapterBoundary(
    episode_id="ep-457dfac97989568e",
    plan_version=3,
    plan_sha256="bb025567cc1d10bb768aa595deec99af503631d6bbd3affbf6224ea487d4ebc3",
    left_item_id="s20",
    right_item_id="s21",
    record_frame=1632,
    source_frame=1845,
    item_count_per_kind=97,
    total_record_frames=7792,
    first_subtitle_id="st21",
    evidence_cue_count=79,
)


@dataclass(frozen=True, slots=True)
class _VerifiedLineage:
    head: HeadState
    plan_sha256: str
    ir_sha256: str


@dataclass(frozen=True, slots=True)
class VerifiedChapterEvidence:
    plan_sha256: str
    ir_sha256: str
    boundary: ChapterBoundaryBinding
    evidence: SubtitleEvidenceSet


def _verified_lineage(
    episode_root: Path, approved: ApprovedChapterBoundary
) -> _VerifiedLineage:
    if not episode_root.is_dir():
        raise ChapterTitleProposalError(
            "lineage-invalid", f"episode root is not a directory: {episode_root}"
        )
    try:
        intake = IntakeRecordV1.model_validate_json(
            (episode_root / "intake.json").read_bytes()
        )
    except (OSError, ValidationError) as error:
        raise ChapterTitleProposalError(
            "lineage-invalid", f"intake is unreadable: {error}"
        ) from error
    if intake.episode_id != approved.episode_id:
        raise ChapterTitleProposalError(
            "lineage-mismatch",
            f"approved episode is {approved.episode_id}, not {intake.episode_id}",
        )
    try:
        log_path, plan_dir = resolve_review_store(episode_root)
        head = load_head(log_path, plan_dir)
    except (FinishingError, ReviewCommitError) as error:
        raise ChapterTitleProposalError("lineage-invalid", str(error)) from error
    if head.version != approved.plan_version:
        raise ChapterTitleProposalError(
            "lineage-mismatch",
            f"approved head is v{approved.plan_version}, sealed store head is v{head.version}",
        )
    entry = head.index.versions[str(head.version)]
    if entry.plan_sha256 != approved.plan_sha256:
        raise ChapterTitleProposalError(
            "lineage-mismatch",
            f"approved plan hash is {approved.plan_sha256}, sealed head is {entry.plan_sha256}",
        )
    return _VerifiedLineage(
        head=head,
        plan_sha256=str(entry.plan_sha256),
        ir_sha256=str(entry.ir_sha256),
    )


def _extract_evidence(
    plan: EditPlan0C, approved: ApprovedChapterBoundary
) -> tuple[ChapterBoundaryBinding, SubtitleEvidenceSet]:
    videos = tuple(item for item in plan.plan.items if item.kind == "video")
    audios = tuple(item for item in plan.plan.items if item.kind == "audio")
    subtitles = tuple(item for item in plan.plan.items if item.kind == "subtitle")
    expected_count = approved.item_count_per_kind
    if (
        len(plan.plan.items) != expected_count * 3
        or len(videos) != expected_count
        or len(audios) != expected_count
        or len(subtitles) != expected_count
    ):
        raise ChapterTitleProposalError(
            "boundary-mismatch",
            "approved plan must contain exactly 97 video, audio, and subtitle items",
        )
    left_position = next(
        (position for position, item in enumerate(videos) if item.item_id == approved.left_item_id),
        None,
    )
    if left_position is None or left_position + 1 >= len(videos):
        raise ChapterTitleProposalError(
            "boundary-mismatch", f"left boundary item {approved.left_item_id} is absent"
        )
    right = videos[left_position + 1]
    if right.item_id != approved.right_item_id:
        raise ChapterTitleProposalError(
            "boundary-mismatch",
            f"{approved.left_item_id} is followed by {right.item_id}, not {approved.right_item_id}",
        )
    record_frame = sum(item.span.length for item in videos[: left_position + 1])
    total_record_frames = sum(item.span.length for item in videos)
    if (
        record_frame != approved.record_frame
        or right.span.start_frame != approved.source_frame
        or total_record_frames != approved.total_record_frames
        or plan.frame_rate.num != FRAME_RATE_NUM
        or plan.frame_rate.den != FRAME_RATE_DEN
        or any(item.span.length <= 0 for item in videos)
    ):
        raise ChapterTitleProposalError(
            "boundary-mismatch",
            f"sealed plan does not reproduce record/source/total frames "
            f"{approved.record_frame}/{approved.source_frame}/{approved.total_record_frames}",
        )
    subtitle_position = next(
        (
            position
            for position, item in enumerate(subtitles)
            if item.item_id == approved.first_subtitle_id
        ),
        None,
    )
    if subtitle_position is None:
        raise ChapterTitleProposalError(
            "boundary-mismatch", f"first evidence cue {approved.first_subtitle_id} is absent"
        )
    evidence_items = subtitles[subtitle_position:]
    suffix_videos = videos[left_position + 1 :]
    if any(not item.item_id.startswith("s") for item in suffix_videos):
        raise ChapterTitleProposalError(
            "boundary-mismatch", "chapter video item IDs do not use the sN review vocabulary"
        )
    expected_subtitles = tuple(f"st{item.item_id[1:]}" for item in suffix_videos)
    if (
        len(evidence_items) != approved.evidence_cue_count
        or tuple(item.item_id for item in evidence_items) != expected_subtitles
        or evidence_items[0].span.start_frame != approved.source_frame
    ):
        raise ChapterTitleProposalError(
            "boundary-mismatch",
            f"subtitle evidence must begin {approved.first_subtitle_id} and contain "
            f"{approved.evidence_cue_count} cues through the end",
        )
    cues: list[SubtitleEvidenceCue] = []
    for item in evidence_items:
        text = item.subtitle_text
        if not text:
            raise ChapterTitleProposalError(
                "boundary-mismatch",
                f"evidence cue {item.item_id} carries no subtitle text",
            )
        cues.append(
            SubtitleEvidenceCue(
                subtitle_id=item.item_id,
                source_start_frame=item.span.start_frame,
                source_end_frame=item.span.end_frame,
                text=text,
            )
        )
    evidence = SubtitleEvidenceSet(cues=tuple(cues))
    boundary = ChapterBoundaryBinding(
        left_item_id=approved.left_item_id,
        right_item_id=approved.right_item_id,
        record_frame=record_frame,
        record_seconds=record_frame / FRAME_RATE_NUM,
        source_frame=right.span.start_frame,
    )
    return boundary, evidence


def load_verified_chapter_evidence(
    episode_root: Path,
    approved: ApprovedChapterBoundary = APPROVED_CHAPTER_BOUNDARY,
) -> VerifiedChapterEvidence:
    """Verify the sealed head and derive evidence without writing to the episode."""

    lineage = _verified_lineage(episode_root, approved)
    boundary, evidence = _extract_evidence(lineage.head.plan, approved)
    return VerifiedChapterEvidence(
        plan_sha256=lineage.plan_sha256,
        ir_sha256=lineage.ir_sha256,
        boundary=boundary,
        evidence=evidence,
    )


__all__ = [
    "APPROVED_CHAPTER_BOUNDARY",
    "ApprovedChapterBoundary",
    "ChapterTitleProposalError",
    "VerifiedChapterEvidence",
    "load_verified_chapter_evidence",
]
