"""Evidence-side helpers for the V44-0 real arm pipeline (task 14 enabler).

Deterministic adapters around the REAL chain outputs, mirroring
``real_pool.py``'s discipline (code over published artifacts, no model
output): the speech-segment → ``MediaIntelligenceArtifact`` v2 build that
feeds DirectorV2's api_v2 index, the neutral approved brief for
``v44-real-01``, the mezzanine→anchor frame-space conversion, and the JP
evidence-quality computation over the operator's corrected transcript
sample (the whisper pin label lives in ``v44_arm_stages``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.editorial_v2.episode_brief import (
    EpisodeBriefV1,
    MustIncludeEntry,
    TargetDurationMinutes,
    approve,
    propose,
)
from services.media_intelligence.models import (
    EditSourceSpan,
    MediaIntelligenceArtifact,
    MediaSource,
    Shot,
    ShotBestMoment,
    ShotConfidence,
    ShotCuttability,
    ShotEditorial,
    ShotVisual,
    TranscriptSegment,
)
from services.metrics.v44_product_proof import (
    EvidenceQualityMetrics,
    GroundTruthAnchor,
    TranscriptSampleV1,
    cer,
    compute_evidence_quality,
)
from services.metrics.v44_product_proof import (
    TranscriptSegment as SampleSegment,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from services.cli.real_pool import SpeechSegment
    from services.contracts.primitives import Identifier

#: Mezzanine is CFR 30/1 (phase-0b target); ground-truth anchors were
#: computed as ``round(ms/1000 * 30000/1001)`` on ORIGINAL ms timestamps.
#: The conform (fps=30 + aresample) preserves the wall-clock timeline, so
#: the same wall time maps: anchor = mezz_frame * 1000/1001.
_ANCHOR_NUM: Final = 1000
_ANCHOR_DEN: Final = 1001
FRAME_SPACE_NOTE: Final = (
    "frame-space: kept spans are Edit Source (CFR 30/1 mezzanine) frames; "
    "ground-truth anchors live in the original 30000/1001 space "
    "(round(ms/1000*30000/1001)); the seam converts kept spans with "
    "round(frame*1000/1001), assuming the conform preserves wall-clock time"
    " (aresample audio, fps=30 video)"
)


class ArmEvidenceError(Exception):
    """Typed evidence-adapter refusal (code/detail)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def mezz_span_to_anchor_space(start_frame: int, end_frame: int) -> tuple[int, int]:
    """Convert a half-open mezzanine span into the anchor 30000/1001 space."""

    return (
        round(start_frame * _ANCHOR_NUM / _ANCHOR_DEN),
        round(end_frame * _ANCHOR_NUM / _ANCHOR_DEN),
    )


def escalated_anchor_ids(
    anchors: Sequence[GroundTruthAnchor], spans: Sequence[tuple[int, int]]
) -> tuple[str, ...]:
    """Anchor ids whose span overlaps any escalated (demoted-keep) span.

    Escalation mapping policy (documented in report notes): a demoted keep
    flags for human review every ground-truth anchor it covers, so the
    metrics count those anchors as escalated rather than silently removed.
    """

    ids: list[str] = []
    for anchor in anchors:
        start, end = int(anchor.start_frame), int(anchor.end_frame)
        if any(s < end and start < e for s, e in spans):
            ids.append(str(anchor.anchor_id))
    return tuple(ids)


def build_speech_mi_artifact(
    episode_id: str,
    source_id: str,
    total_frames: int,
    speech: tuple[SpeechSegment, ...],
) -> MediaIntelligenceArtifact:
    """One Shot per real whisper speech segment; description IS the transcript.

    Honest by construction: no invented visual/editorial facts — the shot
    table is exactly the real transcript evidence the analyzers published,
    in Edit Source frames (``real_pool`` already quantized to the 30fps
    exact-ms lattice). Talking-head single-camera roles come from the H1
    episode contract, not from a vision model.
    """

    shots = tuple(
        Shot(
            shot_id=segment.segment_id,  # type: ignore[arg-type] (s<N> is Identifier-valid)
            source_span=EditSourceSpan(
                start_frame=segment.start_frame, end_frame=segment.end_frame
            ),
            description=segment.text,
            visual=ShotVisual(shot_size="medium", camera_motion="static"),
            editorial=ShotEditorial(
                role="talking_head",
                select_potential="medium",
                best_moment=ShotBestMoment(
                    frame=(segment.start_frame + segment.end_frame) // 2,
                    why="発話区間の中央フレーム",
                ),
                pacing="moderate",
                cuttability=ShotCuttability.model_validate({"in": "clean", "out": "clean"}),
            ),
            transcript_segments=(
                TranscriptSegment(
                    segment_id=segment.segment_id,  # type: ignore[arg-type]
                    text=segment.text,
                    start_frame=segment.start_frame,
                    end_frame=segment.end_frame,
                ),
            ),
            confidence=ShotConfidence(editorial="medium", visual="high"),
        )
        for segment in speech
    )
    return MediaIntelligenceArtifact(
        episode_id=episode_id,  # type: ignore[arg-type]
        sources=(MediaSource(source_id=source_id, duration_frames=total_frames),),  # type: ignore[arg-type]
        shots=shots,
    )


#: Round-3 NO-GO correction A (PRD §6.7 bounded correction): the operator's
#: preservation intent, absent from the round-1/2 briefs while the ground
#: truth wants near-total preservation (75/77 anchors keep). Appended to the
#: objective of EVERY composed arm brief so PassA/PassB — which serialize the
#: whole brief as request DATA — always carry it.
PRESERVATION_GUIDANCE: Final = (
    "カジュアルな一人称vlog。視聴者は作者の素の語り口を楽しむ前提。"
    "編集は「ほぼ全体を残す」方向で、明らかな言い直し・完全な重複・長い沈黙のみ"
    "整理すること。物語構造への再構成（並べ替え・凝縮）はしないこと。"  # noqa: RUF001 (JA guidance, verbatim)
)

_FALLBACK_OBJECTIVE: Final = (
    "v44-real-01 検証エピソード: DJI Pocket 4 の紛失した付属ケースを探す録画を、"
    "編集判断の実証に使う"
)


def compose_arm_brief(episode_root: Path, episode_id: str, operator: str) -> EpisodeBriefV1:
    """Approved brief from the T6 episode.json; neutral fallback for v44-real-01.

    ``brief_draft``/``title_intent`` are honored when the operator filled
    them; otherwise the minimal honest validation-episode brief is composed.
    The operator's preservation guidance (:data:`PRESERVATION_GUIDANCE`,
    round-3 correction A) is appended to the objective in BOTH cases — it is
    the standing intent for this episode, not fallback filler. The
    programmatic approval records the ground-truth operator as the actor.
    """

    draft_text: str | None = None
    title_intent: str | None = None
    try:
        payload: object = json.loads(
            (episode_root / "episode.json").read_text(encoding="utf-8")
        )
        if isinstance(payload, dict):
            raw_draft = payload.get("brief_draft")
            if isinstance(raw_draft, str) and raw_draft.strip():
                draft_text = raw_draft.strip()
            raw_title = payload.get("title_intent")
            if isinstance(raw_title, str) and raw_title.strip():
                title_intent = raw_title.strip()
    except (OSError, ValueError):
        draft_text = None
    objective = f"{draft_text or _FALLBACK_OBJECTIVE}。{PRESERVATION_GUIDANCE}"
    promise = title_intent or "DJI Pocket 4 のケースを探す話を追う短い実験エピソード"
    brief = EpisodeBriefV1(
        episode_id=episode_id,  # type: ignore[arg-type]
        audience_hypothesis="本人と v44 検証を共有する視聴者",
        viewer_promise=promise,
        episode_objective=objective,
        must_include=(MustIncludeEntry(idea_or_moment="DJI Pocket 4 のケースを探す発話"),),
        must_not_misrepresent=(
            "録画内容と異なる編集をしない",
            "物語構造への再構成（並べ替え・凝縮）はしない",  # noqa: RUF001 (JA constraint)
        ),
        target_duration_minutes=TargetDurationMinutes(min=1, max=6),
        pacing_target="moderate",
        editing_intensity="light",
    )
    actor: Identifier = f"operator-{operator}"  # type: ignore[assignment]
    return approve(propose(brief), actor_id=actor)


def _normalize_text(text: str) -> str:
    return "".join(text.split())


_PAIRING_MAX_CER: Final = 0.5


def _pair_timestamps(
    corrected: TranscriptSampleV1,
    hypothesis: Sequence[SampleSegment],
) -> list[float]:
    """Greedy in-order pairing by CER; start-ms diffs for confident pairs only."""

    diffs: list[float] = []
    used: set[int] = set()
    for expected in corrected.segments:
        best_index: int | None = None
        best_cer = _PAIRING_MAX_CER + 1.0
        for index, actual in enumerate(hypothesis):
            if index in used:
                continue
            distance = cer(_normalize_text(expected.text), _normalize_text(actual.text))
            if distance < best_cer:
                best_cer = distance
                best_index = index
        if best_index is None or best_cer > _PAIRING_MAX_CER:
            continue
        used.add(best_index)
        diffs.append(abs(float(expected.start_ms - hypothesis[best_index].start_ms)))
    return diffs


def _hypothesis_proper_nouns(
    corrected: TranscriptSampleV1, hypothesis_text: str
) -> dict[str, str]:
    """Render a term's surface only when the ASR text actually contains it.

    Containment is whitespace-insensitive (whisper spacing is unreliable);
    both the expected surface form and the canonical key count as a hit.
    """

    haystack = _normalize_text(hypothesis_text)
    return {
        canonical: surface
        for canonical, surface in corrected.proper_nouns.items()
        if _normalize_text(surface) in haystack or _normalize_text(canonical) in haystack
    }


def compute_arm_evidence_quality(
    corrected: TranscriptSampleV1,
    hypothesis_ms: Sequence[tuple[int, int, str]],
) -> EvidenceQualityMetrics:
    """JP evidence quality of the real ASR hypothesis vs the corrected sample."""

    hypothesis = tuple(
        SampleSegment(start_ms=start, end_ms=end, text=text)
        for start, end, text in hypothesis_ms
        if text.strip()
    )
    full_text = "".join(text for _s, _e, text in hypothesis_ms)
    return compute_evidence_quality(
        corrected,
        hypothesis,
        hypothesis_proper_nouns=_hypothesis_proper_nouns(corrected, full_text),
        timestamp_diffs_ms=_pair_timestamps(corrected, hypothesis),
    )


__all__ = [
    "FRAME_SPACE_NOTE",
    "PRESERVATION_GUIDANCE",
    "ArmEvidenceError",
    "build_speech_mi_artifact",
    "compose_arm_brief",
    "compute_arm_evidence_quality",
    "escalated_anchor_ids",
    "mezz_span_to_anchor_space",
]
