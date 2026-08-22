"""Deterministic DuckDB index over canonical ``MediaIntelligenceArtifact``s.

The v2 index is REBUILDABLE, never canonical: rows are projections of the
canonical artifact plus the verbatim canonical shot payload (for lossless
``shot_detail``), and every row carries the sha256 content hash of its
parent artifact, computed exactly like v1 (``canonical_model_bytes``).
Determinism contract: identical artifacts yield IDENTICAL ORDERED ROW SETS
(shots sorted by ``(start_frame, end_frame, shot_id)``; child rows follow
shot order). DuckDB file BYTES are not claimed deterministic and are never
evidence. ``build_index`` is the ONLY writer; the query surface opens the
file read-only. Per-shot ``source_id`` attribution is stored only when the
artifact declares exactly one source — multi-source artifacts leave it
NULL rather than fabricate a linkage the canonical model does not carry.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import duckdb

from services.foundation_io import canonical_model_bytes
from services.media_intelligence.models import MediaIntelligenceArtifact, Shot
from services.media_intelligence.moment_review import MomentDeepReviewV1, review_rows
from services.media_query.v2_models import ApiIndexErrorV2

V2_SCHEMA_MARKER: Final = "media-intelligence-v2-index"
V2_INDEX_TABLES: Final[tuple[str, ...]] = (
    "mi_meta",
    "mi_sources",
    "mi_shots",
    "mi_transcripts",
    "mi_visible_texts",
    "mi_quality",
    "mi_audio",
    "mi_similarity",
    "mi_search_texts",
    "mi_moment_reviews",
)

_V2_DDL: Final[tuple[str, ...]] = (
    "CREATE TABLE mi_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE mi_sources (episode_id TEXT NOT NULL, source_id TEXT NOT NULL,
        duration_frames BIGINT, artifact_sha TEXT NOT NULL,
        PRIMARY KEY (source_id, artifact_sha))""",
    """CREATE TABLE mi_shots (episode_id TEXT NOT NULL, shot_id TEXT NOT NULL,
        artifact_sha TEXT NOT NULL, source_id TEXT, start_frame BIGINT NOT NULL,
        end_frame BIGINT NOT NULL, description TEXT NOT NULL, shot_size TEXT NOT NULL,
        camera_motion TEXT NOT NULL, framing TEXT, role TEXT NOT NULL,
        select_potential TEXT NOT NULL, pacing TEXT NOT NULL, best_moment_frame BIGINT NOT NULL,
        best_moment_why TEXT NOT NULL, confidence_editorial TEXT NOT NULL,
        confidence_visual TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY (shot_id, artifact_sha))""",
    """CREATE TABLE mi_transcripts (shot_id TEXT NOT NULL, artifact_sha TEXT NOT NULL,
        seq INTEGER NOT NULL, segment_id TEXT NOT NULL, start_frame BIGINT NOT NULL,
        end_frame BIGINT NOT NULL, text TEXT NOT NULL,
        PRIMARY KEY (shot_id, artifact_sha, seq))""",
    """CREATE TABLE mi_visible_texts (shot_id TEXT NOT NULL, artifact_sha TEXT NOT NULL,
        seq INTEGER NOT NULL, text TEXT NOT NULL, frame BIGINT,
        PRIMARY KEY (shot_id, artifact_sha, seq))""",
    """CREATE TABLE mi_quality (shot_id TEXT NOT NULL, artifact_sha TEXT NOT NULL,
        start_frame BIGINT NOT NULL, end_frame BIGINT NOT NULL, flag TEXT NOT NULL,
        severity TEXT, PRIMARY KEY (shot_id, artifact_sha, flag))""",
    """CREATE TABLE mi_audio (shot_id TEXT NOT NULL, artifact_sha TEXT NOT NULL,
        start_frame BIGINT NOT NULL, end_frame BIGINT NOT NULL, loudness_db DOUBLE,
        energy DOUBLE, ambient_type TEXT, PRIMARY KEY (shot_id, artifact_sha))""",
    """CREATE TABLE mi_similarity (shot_id TEXT NOT NULL, artifact_sha TEXT NOT NULL,
        seq INTEGER NOT NULL, ref_shot_id TEXT NOT NULL, score DOUBLE,
        PRIMARY KEY (shot_id, artifact_sha, seq))""",
    """CREATE TABLE mi_search_texts (shot_id TEXT NOT NULL, artifact_sha TEXT NOT NULL,
        seq INTEGER NOT NULL, field TEXT NOT NULL, text TEXT NOT NULL,
        PRIMARY KEY (shot_id, artifact_sha, seq))""",
    """CREATE TABLE mi_moment_reviews (episode_id TEXT NOT NULL, review_id TEXT NOT NULL,
        artifact_sha TEXT NOT NULL, start_frame BIGINT NOT NULL, end_frame BIGINT NOT NULL,
        prev_shot_id TEXT, next_shot_id TEXT, overall_confidence DOUBLE NOT NULL,
        provider TEXT NOT NULL, provider_version TEXT NOT NULL, tool TEXT NOT NULL,
        PRIMARY KEY (review_id, artifact_sha))""",
)


def artifact_content_sha(artifact: MediaIntelligenceArtifact) -> str:
    return hashlib.sha256(canonical_model_bytes(artifact)).hexdigest()


def _executemany(
    connection: duckdb.DuckDBPyConnection, sql: str, rows: list[tuple[object, ...]]
) -> None:
    if rows:  # duckdb executemany rejects an empty parameter list; empty means "no rows"
        connection.executemany(sql, rows)


def _search_texts(shot: Shot) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = [("description", shot.description)]
    pairs.extend(("transcript", seg.text) for seg in shot.transcript_segments or ())
    pairs.extend(("visible_text", vt.text) for vt in shot.visible_texts or ())
    if shot.location is not None:
        pairs.append(("location", shot.location.location))
        if shot.location.description is not None:
            pairs.append(("location", shot.location.description))
    if shot.subject_action is not None:
        pairs.append(("subject", shot.subject_action.primary_subject))
        pairs.append(("action", shot.subject_action.action))
    return tuple(pairs)


def _quality_rows(shot: Shot) -> tuple[tuple[str, str | None], ...]:
    flags = tuple(shot.visual.quality_flags)
    rows: list[tuple[str, str | None]] = [(flag, None) for flag in flags]
    detail = shot.visual_quality_detail
    if detail is not None and detail.issue not in flags:
        rows.append((detail.issue, detail.severity))
    return tuple(rows)


def _insert_artifact_rows(
    connection: duckdb.DuckDBPyConnection, artifact: MediaIntelligenceArtifact, sha: str
) -> None:
    source_id = str(artifact.sources[0].source_id) if len(artifact.sources) == 1 else None
    _executemany(
        connection,
        "INSERT INTO mi_sources VALUES (?, ?, ?, ?)",
        [
            (str(artifact.episode_id), str(src.source_id), src.duration_frames, sha)
            for src in sorted(artifact.sources, key=lambda item: str(item.source_id))
        ],
    )
    shots = sorted(
        artifact.shots,
        key=lambda item: (
            int(item.source_span.start_frame),
            int(item.source_span.end_frame),
            str(item.shot_id),
        ),
    )
    _executemany(
        connection,
        "INSERT INTO mi_shots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                str(artifact.episode_id),
                str(shot.shot_id),
                sha,
                source_id,
                int(shot.source_span.start_frame),
                int(shot.source_span.end_frame),
                shot.description,
                shot.visual.shot_size,
                shot.visual.camera_motion,
                shot.visual.framing,
                shot.editorial.role,
                shot.editorial.select_potential,
                shot.editorial.pacing,
                int(shot.editorial.best_moment.frame),
                shot.editorial.best_moment.why,
                shot.confidence.editorial,
                shot.confidence.visual,
                canonical_model_bytes(shot).decode(),
            )
            for shot in shots
        ],
    )
    for shot in shots:
        key = (str(shot.shot_id), sha)
        _executemany(
            connection,
            "INSERT INTO mi_transcripts VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (*key, seq, str(seg.segment_id), int(seg.start_frame), int(seg.end_frame), seg.text)
                for seq, seg in enumerate(shot.transcript_segments or ())
            ],
        )
        _executemany(
            connection,
            "INSERT INTO mi_visible_texts VALUES (?, ?, ?, ?, ?)",
            [(*key, seq, vt.text, vt.frame) for seq, vt in enumerate(shot.visible_texts or ())],
        )
        _executemany(
            connection,
            "INSERT INTO mi_quality VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    *key,
                    int(shot.source_span.start_frame),
                    int(shot.source_span.end_frame),
                    flag,
                    severity,
                )
                for flag, severity in _quality_rows(shot)
            ],
        )
        if shot.audio_measurements is not None:
            audio = shot.audio_measurements
            connection.execute(
                "INSERT INTO mi_audio VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    *key,
                    int(shot.source_span.start_frame),
                    int(shot.source_span.end_frame),
                    audio.loudness_db,
                    audio.energy,
                    audio.ambient_type,
                ],
            )
        _executemany(
            connection,
            "INSERT INTO mi_similarity VALUES (?, ?, ?, ?, ?)",
            [
                (*key, seq, str(ref.ref_shot_id), ref.score)
                for seq, ref in enumerate(shot.similarity_refs or ())
            ],
        )
        _executemany(
            connection,
            "INSERT INTO mi_search_texts VALUES (?, ?, ?, ?, ?)",
            [(*key, seq, field, text) for seq, (field, text) in enumerate(_search_texts(shot))],
        )


def open_read_only(index_path: Path) -> duckdb.DuckDBPyConnection:
    """Open a v2 index read-only, refusing non-v2 files (typed ApiIndexErrorV2)."""

    try:
        connection = duckdb.connect(str(index_path), read_only=True)
    except duckdb.Error as error:
        raise ApiIndexErrorV2("index-unreadable", str(error)) from error
    try:
        row = connection.execute("SELECT value FROM mi_meta WHERE key = 'schema'").fetchone()
    except duckdb.Error as error:
        connection.close()
        raise ApiIndexErrorV2("not-a-v2-index", str(error)) from error
    if row is None or str(row[0]) != V2_SCHEMA_MARKER:
        connection.close()
        raise ApiIndexErrorV2("not-a-v2-index", "mi_meta schema marker missing/mismatched")
    return connection


def build_index(
    artifacts: MediaIntelligenceArtifact | Sequence[MediaIntelligenceArtifact],
    db_path: Path,
    *,
    reviews: Sequence[MomentDeepReviewV1] = (),
) -> Path:
    """Build (or idempotently rebuild) the v2 index and return its path.

    ``reviews`` is the task-17 additive input: committed
    ``MomentDeepReviewV1`` artifacts indexed as ``mi_moment_reviews`` rows
    (deterministically ordered via :func:`review_rows`). Indexes built
    without reviews simply leave that table empty; the query surface treats
    a missing table as zero reviews for pre-task-17 files.
    """

    batch = [artifacts] if isinstance(artifacts, MediaIntelligenceArtifact) else list(artifacts)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(db_path))
    try:
        connection.execute("BEGIN TRANSACTION")
        for table in V2_INDEX_TABLES:
            # table names come from the frozen V2_INDEX_TABLES constant, never callers
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        for statement in _V2_DDL:
            connection.execute(statement)
        connection.execute("INSERT INTO mi_meta VALUES ('schema', ?)", [V2_SCHEMA_MARKER])
        for artifact in batch:
            _insert_artifact_rows(connection, artifact, artifact_content_sha(artifact))
        _executemany(
            connection,
            "INSERT INTO mi_moment_reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            list(review_rows(reviews)),
        )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    return db_path


__all__ = [
    "V2_INDEX_TABLES",
    "V2_SCHEMA_MARKER",
    "artifact_content_sha",
    "build_index",
    "open_read_only",
]
