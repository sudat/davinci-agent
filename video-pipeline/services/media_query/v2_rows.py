"""Row narrowing, deterministic scoring, and aggregation for the v2 query surface.

Pure functions over rows already fetched from the v2 index: narrowing of
DuckDB ``object`` columns to strict types, response-row builders, the
keyword-scoring kernel of ``semantic_shot_search`` (ja substring match +
en case-folded occurrence counts — deterministic, embeddings are NOT
simulated), and the scene-summary aggregation. No SQL and no I/O live
here; ``query_v2`` owns statements and budgets.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.media_query import v2_models as vm
from services.media_query.queries import _optional_int
from services.media_query.queries import _require_int as _int
from services.media_query.queries import _require_str as _str

_FetchedRow = tuple[object, ...]


def require_float(value: object) -> float:
    if isinstance(value, float | int) and not isinstance(value, bool):
        return float(value)
    raise TypeError(f"index column must hold a number, got {type(value).__name__}")


def optional_float(value: object) -> float | None:
    return None if value is None else require_float(value)


def shot_row(r: _FetchedRow) -> vm.ShotRow:
    return vm.ShotRow(
        shot_id=_str(r[0]), artifact_sha=_str(r[1]),
        span=vm.FrameSpan(start_frame=_int(r[2]), end_frame=_int(r[3])),
        description=_str(r[4]), shot_size=_str(r[5]), camera_motion=_str(r[6]),
        framing=None if r[7] is None else _str(r[7]), role=_str(r[8]),
        select_potential=_str(r[9]), pacing=_str(r[10]),
        confidence_editorial=_str(r[11]), confidence_visual=_str(r[12]))


def best_moment_row(r: _FetchedRow) -> vm.BestMomentRow:
    return vm.BestMomentRow(
        frame=_int(r[0]), why=_str(r[1]), shot_id=_str(r[2]),
        span=vm.FrameSpan(start_frame=_int(r[3]), end_frame=_int(r[4])),
        role=_str(r[5]), select_potential=_str(r[6]), artifact_sha=_str(r[7]))


def transcript_range_row(r: _FetchedRow) -> vm.TranscriptRangeRow:
    return vm.TranscriptRangeRow(
        segment_id=_str(r[0]),
        span=vm.FrameSpan(start_frame=_int(r[1]), end_frame=_int(r[2])),
        text=_str(r[3]), shot_id=_str(r[4]), artifact_sha=_str(r[5]))


def visible_text_row(r: _FetchedRow) -> vm.VisibleTextRow:
    return vm.VisibleTextRow(
        shot_id=_str(r[0]), artifact_sha=_str(r[1]),
        span=vm.FrameSpan(start_frame=_int(r[2]), end_frame=_int(r[3])),
        text=_str(r[4]), frame=_optional_int(r[5]))


def quality_row(r: _FetchedRow) -> vm.QualityRangeV2Row:
    return vm.QualityRangeV2Row(
        shot_id=_str(r[0]), artifact_sha=_str(r[1]),
        span=vm.FrameSpan(start_frame=_int(r[2]), end_frame=_int(r[3])),
        flag=_str(r[4]), severity=None if r[5] is None else _str(r[5]))


def audio_energy_row(r: _FetchedRow) -> vm.AudioEnergyRow:
    return vm.AudioEnergyRow(
        shot_id=_str(r[0]), artifact_sha=_str(r[1]),
        span=vm.FrameSpan(start_frame=_int(r[2]), end_frame=_int(r[3])),
        energy=require_float(r[4]), loudness_db=optional_float(r[5]),
        ambient_type=None if r[6] is None else _str(r[6]))


def similar_shot_row(r: _FetchedRow, shot_id: str) -> vm.SimilarShotRow:
    return vm.SimilarShotRow(
        shot_id=shot_id, ref_shot_id=_str(r[0]), score=optional_float(r[1]),
        artifact_sha=_str(r[2]))


def moment_review_row(r: _FetchedRow) -> vm.MomentReviewRow:
    return vm.MomentReviewRow(
        review_id=_str(r[0]), episode_id=_str(r[1]), artifact_sha=_str(r[2]),
        span=vm.FrameSpan(start_frame=_int(r[3]), end_frame=_int(r[4])),
        prev_shot_id=None if r[5] is None else _str(r[5]),
        next_shot_id=None if r[6] is None else _str(r[6]),
        overall_confidence=require_float(r[7]), provider=_str(r[8]),
        provider_version=_str(r[9]), tool=_str(r[10]))


@dataclass(frozen=True, slots=True)
class SemanticHit:
    score: int
    start_frame: int
    shot_id: str
    artifact_sha: str
    candidate: _FetchedRow
    matched_fields: tuple[str, ...]

    @property
    def sort_key(self) -> tuple[int, int, str, str]:
        return (-self.score, self.start_frame, self.shot_id, self.artifact_sha)


def semantic_ranked_hits(
    candidates: tuple[_FetchedRow, ...],
    text_rows: tuple[_FetchedRow, ...],
    keywords: list[str],
) -> tuple[SemanticHit, ...]:
    texts: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for row in text_rows:
        texts.setdefault((_str(row[0]), _str(row[1])), []).append((_str(row[2]), _str(row[3])))
    folded_keywords = [keyword.casefold() for keyword in keywords]
    hits = []
    for candidate in candidates:
        fields = texts.get((_str(candidate[0]), _str(candidate[1])), [])
        score, matched = 0, set()
        for field, text in fields:
            folded = text.casefold()
            for keyword in folded_keywords:
                occurrences = folded.count(keyword)
                if occurrences:
                    score += occurrences
                    matched.add(field)
        if score:
            hits.append(SemanticHit(
                score=score, start_frame=_int(candidate[2]), shot_id=_str(candidate[0]),
                artifact_sha=_str(candidate[1]), candidate=candidate,
                matched_fields=tuple(sorted(matched))))
    hits.sort(key=lambda hit: hit.sort_key)
    return tuple(hits)


def semantic_hit_row(hit: SemanticHit) -> vm.SemanticHitRow:
    return vm.SemanticHitRow(
        shot_id=hit.shot_id, artifact_sha=hit.artifact_sha,
        span=vm.FrameSpan(start_frame=_int(hit.candidate[2]), end_frame=_int(hit.candidate[3])),
        score=hit.score, matched_fields=hit.matched_fields, description=_str(hit.candidate[4]))


def scene_summary_rows(
    episodes: tuple[_FetchedRow, ...],
    role_rows: tuple[_FetchedRow, ...],
    size_rows: tuple[_FetchedRow, ...],
    source_rows: tuple[_FetchedRow, ...],
    sha_rows: tuple[_FetchedRow, ...],
) -> tuple[vm.SceneSummaryRow, ...]:
    roles: dict[str, list[vm.NameCount]] = {}
    for row in role_rows:
        roles.setdefault(_str(row[0]), []).append(
            vm.NameCount(name=_str(row[1]), count=_int(row[2])))
    sizes: dict[str, list[vm.NameCount]] = {}
    for row in size_rows:
        sizes.setdefault(_str(row[0]), []).append(
            vm.NameCount(name=_str(row[1]), count=_int(row[2])))
    source_counts = {_str(row[0]): _int(row[1]) for row in source_rows}
    shas: dict[str, list[str]] = {}
    for row in sha_rows:
        shas.setdefault(_str(row[0]), []).append(_str(row[1]))
    return tuple(
        vm.SceneSummaryRow(
            episode_id=_str(row[0]), shot_count=_int(row[1]),
            covered_frames=_int(row[2] or 0), source_count=source_counts.get(_str(row[0]), 0),
            role_counts=tuple(roles.get(_str(row[0]), ())),
            shot_size_counts=tuple(sizes.get(_str(row[0]), ())),
            artifact_shas=tuple(shas.get(_str(row[0]), ())))
        for row in episodes)


__all__ = [
    "SemanticHit", "audio_energy_row", "best_moment_row", "moment_review_row", "optional_float",
    "quality_row", "require_float", "scene_summary_rows", "semantic_hit_row",
    "semantic_ranked_hits", "shot_row", "similar_shot_row", "transcript_range_row",
    "visible_text_row",
]
