"""DuckDB media-query index: rebuild, lineage, rollback, read-only behavior.

Golden expectations are PRE-REGISTERED from the synthesis parameters (the
Todo 33-35 stacks on designed inputs):

- designed audio 48 kHz (tone 5 s / silence 0.5 s / tone 7 s) has exactly one
  silence span [240000, 264000) samples = [5000, 5500) ms, dialogue RMS
  -6021 mB, ambient floor -120000 mB, peak 16384 samples (-6021 mB);
- designed video (80 CFR30 frames: black/bars/blurred-bars/white/bars) has
  black span [0,10), blur span [30,50), over-exposure span [50,60), and one
  contact sheet of frames 0,10,...,70 in a 5x2 grid of 64x36 thumbs.

Determinism contract (asserted here): the DuckDB file is a REBUILDABLE
index; determinism is claimed at ROW level only (identical ordered row sets
across rebuilds), never at file-byte level.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pytest

import services.media_query.index as index_module
from services.analyze.analysis_models import StreamFacts
from services.analyze.asr_models import (
    AsrInputBinding,
    AsrSettingsEcho,
    ExperimentalTiming,
    ExperimentalToken,
    SegmentTokenTiming,
    TranscriptArtifact,
    TranscriptSegment,
    WavProbeSummary,
    transcript_content_hash,
)
from services.analyze.audio_analysis import AudioAnalysisRequest, analyze_dialogue
from services.analyze.visual_analysis import (
    VisualAnalysisRequest,
    analyze_visual,
)
from services.artifact_registry.registry import ArtifactRegistry
from services.artifact_store.models import PublicationIntent, PublicationReceipt
from services.artifact_store.store import ArtifactStore
from services.contracts.primitives import ArtifactEnvelope, Producer
from services.foundation_io import canonical_model_bytes, sha256_file
from services.media_query.index import MEDIA_DB_NAME, MediaQueryError, MediaQueryIndex
from services.media_query.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    Migration,
    apply_migrations,
)
from services.media_query.queries import (
    find_quality_ranges,
    find_silence_ranges,
    find_transcript_segments,
    get_contact_refs,
    get_statistics,
)
from services.toolchain.models import Phase1TechnicalToolchainLock, load_lock

if TYPE_CHECKING:
    from services.analyze.candidate_models import AnalysisArtifact
    from services.analyze.visual_models import VisualAnalysisArtifact
    from services.media_query.index import AnalyzerArtifact

# --- pre-registered golden values (from synthesis parameters only) ---
MB_SQUARE_HALF = -6021  # 10000*log10(0.25) rounded half away
MB_FLOOR = -120000  # digital-silence floor
GOLDEN_SILENCE_SPAN = (240000, 264000)  # samples at 48 kHz
GOLDEN_SILENCE_MS = (5000, 5500)
GOLDEN_BLACK_SPAN = (0, 10)
GOLDEN_BLUR_SPAN = (30, 50)
GOLDEN_EXPOSURE_SPAN = (50, 60)
GOLDEN_SHEET_FRAMES = (0, 10, 20, 30, 40, 50, 60, 70)
GOLDEN_SHEET_LAYOUT = (5, 2, 64, 36)  # cols, rows, thumb_w, thumb_h
GOLDEN_FRAME_COUNT = 80
TRANSCRIPT_SEGMENTS = (
    (0, 5000, "今日は撮影の裏側をお見せします。"),
    (5000, 5500, ""),
    (5500, 12500, "まずカメラのセッティングからです。"),
)

PHASE1_LOCK = Path("config/toolchains/phase-1-technical-v1.json")
SQUARE = r"if(lt(mod(t*440\,1)\,0.5)\,0.5\,-0.5)"
SEGMENT_GRAPHS = (
    ("color=black:s=320x180:r=30", 10),
    ("smptebars=s=320x180:r=30", 20),
    ("smptebars=s=320x180:r=30,gblur=sigma=8", 20),
    ("color=white:s=320x180:r=30", 10),
)

DATA_TABLES = (
    "sources",
    "transcript_segments",
    "sample_spans",
    "silence_ranges",
    "quality_ranges",
    "contact_refs",
    "statistics",
    "lineage",
)


@dataclass(frozen=True, slots=True)
class EpisodeEnv:
    store: ArtifactStore
    registry: ArtifactRegistry
    episode_dir: Path
    transcript: TranscriptArtifact
    analysis: AnalysisArtifact
    visual: VisualAnalysisArtifact
    receipts: tuple[PublicationReceipt, ...]
    sheet_paths: tuple[str, ...]


def _run_ffmpeg(ffmpeg: Path, argv: tuple[str, ...], out: Path) -> None:
    result = subprocess.run(
        (str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *argv, str(out)),
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert out.is_file()


@pytest.fixture(scope="session")
def lock() -> Phase1TechnicalToolchainLock:
    loaded = load_lock(PHASE1_LOCK)
    assert isinstance(loaded, Phase1TechnicalToolchainLock)
    if not Path(loaded.ffmpeg.ffmpeg.path).is_file() or not Path(
        loaded.ffmpeg.ffprobe.path
    ).is_file():
        pytest.skip("pinned ffmpeg/ffprobe not bootstrapped")
    return loaded


def _build_transcript(
    segments: tuple[tuple[int, int, str], ...] = TRANSCRIPT_SEGMENTS,
    *,
    producer_version: str = "todo33-v1",
) -> TranscriptArtifact:
    binding = AsrInputBinding(
        media_path="/var/media/episode.m4v",
        media_sha256="b" * 64,
        wav_sha256="a" * 64,
        wav_probe=WavProbeSummary(),
    )
    parsed = tuple(
        TranscriptSegment(start_ms=start, end_ms=end, text=text)
        for start, end, text in segments
    )
    tokens = SegmentTokenTiming(
        segment_index=0,
        tokens=(ExperimentalToken(text="今日", start_ms=0, end_ms=500),),
    )
    experimental = ExperimentalTiming(
        label="experimental-token-timing",
        token_level=(tokens,),
        consistent_with_segments=True,
    )
    echo = AsrSettingsEcho(
        ffmpeg_argv=("/usr/local/bin/ffmpeg", "-nostdin"),
        whisper_argv=("/usr/local/bin/whisper-cli", "--model", "/models/ggml.bin"),
    )
    content_hash = transcript_content_hash(binding, parsed, experimental, echo)
    return TranscriptArtifact(
        artifact_id=f"transcript-{content_hash[:16]}",
        artifact_type="transcript_asr_whisper_cpp",
        schema_version="asr-transcript-v1",
        content_hash=content_hash,
        producer=Producer(name="asr-whisper-cpp", version=producer_version),
        inputs=(),
        input_binding=binding,
        segments=parsed,
        experimental=experimental,
        settings_echo=echo,
    )


@pytest.fixture(scope="session")
def episode(
    lock: Phase1TechnicalToolchainLock, tmp_path_factory: pytest.TempPathFactory
) -> EpisodeEnv:
    """One episode: real Todo 33-35 artifacts on tiny synthesized inputs."""
    root = tmp_path_factory.mktemp("todo36-episode")
    ffmpeg = Path(lock.ffmpeg.ffmpeg.path)
    transcript = _build_transcript()

    designed48 = root / "designed48.wav"
    _run_ffmpeg(
        ffmpeg,
        (
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc=exprs={SQUARE}:s=48000:d=5",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=exprs=0:s=48000:d=0.5",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc=exprs={SQUARE}:s=48000:d=7",
            "-filter_complex",
            "[0:a][1:a][2:a]concat=n=3:v=0:a=1",
            "-c:a",
            "pcm_s16le",
        ),
        designed48,
    )
    with wave.open(str(designed48)) as stream:
        assert stream.getnframes() == 600000
    analyzed = analyze_dialogue(
        AudioAnalysisRequest(
            wav_path=str(designed48),
            wav_sha256=sha256_file(designed48),
            declared=StreamFacts(sample_rate=48000, channels=1, codec="pcm_s16le"),
            transcript=transcript,
        ),
        work_dir=root / "audio-work",
        cache_dir=root / "audio-cache",
    )
    analysis = analyzed.artifact
    assert analysis.producer.name == "analyze-dialogue"

    segments = []
    for index, (graph, frames) in enumerate(SEGMENT_GRAPHS):
        segment = root / f"vseg{index}.avi"
        _run_ffmpeg(
            ffmpeg,
            ("-f", "lavfi", "-i", graph, "-frames:v", str(frames), "-c:v", "ffv1"),
            segment,
        )
        segments.append(segment)
    sharp_again = root / "vseg4.avi"
    shutil.copyfile(segments[1], sharp_again)
    segments.append(sharp_again)
    chain = "".join(f"[{index}:v]" for index in range(len(segments)))
    designed_avi = root / "designed.avi"
    _run_ffmpeg(
        ffmpeg,
        (
            *[part for segment in segments for part in ("-i", str(segment))],
            "-filter_complex",
            f"{chain}concat=n={len(segments)}:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "ffv1",
        ),
        designed_avi,
    )
    visualized = analyze_visual(
        VisualAnalysisRequest(
            media_path=str(designed_avi),
            media_sha256=sha256_file(designed_avi),
            fixture_only=True,
        ),
        work_dir=root / "visual-work",
    )
    visual = visualized.artifact
    assert visual.producer.name == "analyze-visual"
    assert visual.media.facts.frame_count == GOLDEN_FRAME_COUNT

    store = ArtifactStore(root / "store")
    registry = ArtifactRegistry(root / "registry")
    receipts = []
    for artifact in (transcript, analysis, visual):
        payload = canonical_model_bytes(artifact)
        envelope = ArtifactEnvelope(
            artifact_id=artifact.artifact_id,
            artifact_type=artifact.artifact_type,
            schema_version=artifact.schema_version,
            content_hash=hashlib.sha256(payload).hexdigest(),
            producer=artifact.producer,
            inputs=artifact.inputs,
        )
        receipt = store.publish(PublicationIntent(envelope=envelope), payload)
        receipts.append(registry.register(store, receipt))

    episode_dir = root / "jobs" / "ep-001"
    episode_dir.mkdir(parents=True)
    return EpisodeEnv(
        store=store,
        registry=registry,
        episode_dir=episode_dir,
        transcript=transcript,
        analysis=analysis,
        visual=visual,
        receipts=tuple(receipts),
        sheet_paths=visualized.sheet_paths,
    )


def count_scalar(
    connection: duckdb.DuckDBPyConnection, sql: str, params: list[str] | None = None
) -> int:
    row = connection.execute(sql, params).fetchone()
    assert row is not None
    value = row[0]
    assert isinstance(value, int)
    return value


def dump_tables(connection: duckdb.DuckDBPyConnection) -> dict[str, list[tuple[object, ...]]]:
    order = {
        "sources": "ORDER BY source_id, artifact_sha",
        "transcript_segments": "ORDER BY source_id, segment_index, artifact_sha",
        "sample_spans": "ORDER BY source_id, kind, segment_index, start_sample, artifact_sha",
        "silence_ranges": "ORDER BY source_id, kind, start_sample, end_sample, artifact_sha",
        "quality_ranges": "ORDER BY source_id, kind, start_frame, end_frame, artifact_sha",
        "contact_refs": "ORDER BY source_id, sheet_sha256, artifact_sha",
        "statistics": "ORDER BY source_id, metric, artifact_sha",
        "lineage": "ORDER BY artifact_sha",
    }
    dumped: dict[str, list[tuple[object, ...]]] = {}
    for table in DATA_TABLES:
        # table names come from the module DATA_TABLES constant, never callers
        rows = connection.execute(f"SELECT * FROM {table} {order[table]}").fetchall()  # noqa: S608
        dumped[table] = [tuple(row) for row in rows]
    return dumped


@pytest.fixture
def index(episode: EpisodeEnv) -> MediaQueryIndex:
    opened = MediaQueryIndex.open(episode.episode_dir / MEDIA_DB_NAME)
    opened.rebuild(episode.store, episode.registry, episode.episode_dir)
    return opened


# ---------------------------------------------------------------- happy


def test_migrations_apply_and_validate_history(tmp_path: Path) -> None:
    path = tmp_path / MEDIA_DB_NAME
    first = MediaQueryIndex.open(path)
    applied = [
        int(row[0])
        for row in first.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    ]
    assert applied == list(range(1, LATEST_VERSION + 1))
    tables = {
        str(row[0])
        for row in first.connection.execute("SHOW TABLES").fetchall()
    }
    assert set(DATA_TABLES) <= tables
    first.close()

    # tampered migration hash -> refuse to open
    tamper = duckdb.connect(path)
    tamper.execute("UPDATE schema_migrations SET sha256 = ? WHERE version = 1", ["0" * 64])
    tamper.close()
    with pytest.raises(Exception, match="migration-hash-mismatch"):
        MediaQueryIndex.open(path)

    # unknown future version -> refuse to open (on an unmodified database)
    future_source = tmp_path / "future-source.duckdb"
    seeded = MediaQueryIndex.open(future_source)
    seeded.close()
    future = duckdb.connect(future_source)
    future.execute("INSERT INTO schema_migrations VALUES (99, 'ghost', '1' * 64)")
    future.close()
    with pytest.raises(Exception, match="unknown-schema-version"):
        MediaQueryIndex.open(future_source)


def test_interrupted_migration_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / MEDIA_DB_NAME
    base = MediaQueryIndex.open(path)
    base.close()
    bad = Migration(
        version=2,
        name="always-fails",
        statements=(
            "CREATE TABLE halfway (id INTEGER)",
            "CREATE TABLE halfway (id INTEGER, dup INTEGER)",  # duplicate -> fault
        ),
    )
    def apply_bad() -> None:
        connection = duckdb.connect(path)
        try:
            apply_migrations(connection, (MIGRATIONS[0], bad))
        finally:
            connection.close()

    with pytest.raises(Exception, match="migration-failed"):
        apply_bad()
    reopened = MediaQueryIndex.open(path)
    tables = {str(row[0]) for row in reopened.connection.execute("SHOW TABLES").fetchall()}
    assert "halfway" not in tables
    reopened.close()


def test_rebuild_indexes_all_rows_with_lineage(episode: EpisodeEnv, index: MediaQueryIndex) -> None:
    connection = index.connection
    lineage_rows = connection.execute("SELECT * FROM lineage ORDER BY artifact_sha").fetchall()
    assert len(lineage_rows) == 3
    types = {str(row[1]) for row in lineage_rows}
    assert types == {
        "transcript_asr_whisper_cpp",
        "analysis_dialogue_candidates",
        "analysis_visual_minimum",
    }
    analyzer_versions = {str(row[5]) for row in lineage_rows}
    assert analyzer_versions == {"todo33-v1", "todo34-v1", "todo35-v1"}
    # every data row carries an artifact_sha resolving to a lineage parent
    for table in DATA_TABLES[:-1]:
        # table names come from the module DATA_TABLES constant, never callers
        orphans = count_scalar(
            connection,
            f"SELECT count(*) FROM {table} "  # noqa: S608
            "WHERE artifact_sha NOT IN (SELECT artifact_sha FROM lineage)",
        )
        assert orphans == 0, table
    # lineage row_count matches the actual per-artifact row totals
    for row in lineage_rows:
        artifact_sha = str(row[3])
        declared = int(row[6])
        actual = 0
        for table in DATA_TABLES[:-1]:
            actual += count_scalar(
                connection,
                # table names come from the module DATA_TABLES constant, never callers
                f"SELECT count(*) FROM {table} WHERE artifact_sha = ?",  # noqa: S608
                [artifact_sha],
            )
        assert declared == actual
    assert all(
        # table names come from the module DATA_TABLES constant, never callers
        count_scalar(connection, f"SELECT count(*) FROM {table}") > 0  # noqa: S608
        for table in DATA_TABLES[:-1]
    )


def test_transcript_overlap_query_exact(episode: EpisodeEnv, index: MediaQueryIndex) -> None:
    first_two = find_transcript_segments(index.connection, "edit-source-audio", 4500, 5100)
    assert [(row.segment_index, row.start_ms, row.end_ms) for row in first_two] == [
        (0, 0, 5000),
        (1, 5000, 5500),
    ]
    assert first_two[0].text == TRANSCRIPT_SEGMENTS[0][2]
    assert first_two[0].analyzer_version == "todo33-v1"
    assert first_two[0].artifact_sha == episode.receipts[0].content_sha256
    only_first = find_transcript_segments(index.connection, "edit-source-audio", 0, 5000)
    assert [row.segment_index for row in only_first] == [0]
    empty = find_transcript_segments(index.connection, "edit-source-audio", 20000, 30000)
    assert empty == ()


def test_silence_range_query_exact(index: MediaQueryIndex) -> None:
    both = find_silence_ranges(index.connection, "edit-source-audio", 250000, 260000)
    kinds = sorted(row.kind for row in both)
    assert kinds == ["measured_silence", "pause"]
    for row in both:
        assert (row.start_sample, row.end_sample) == GOLDEN_SILENCE_SPAN
        assert (row.start_ms, row.end_ms) == GOLDEN_SILENCE_MS
        assert row.sample_rate == 48000
    pause = next(row for row in both if row.kind == "pause")
    assert pause.confidence == 700
    assert pause.rule_id == "pause-silence-v1"
    measured = next(row for row in both if row.kind == "measured_silence")
    assert measured.confidence is None
    assert measured.rule_id is None
    # half-open span semantics
    assert find_silence_ranges(index.connection, "edit-source-audio", 0, 240000) == ()
    assert find_silence_ranges(index.connection, "edit-source-audio", 264000, 300000) == ()
    assert len(find_silence_ranges(index.connection, "edit-source-audio", 263999, 264001)) == 2


def test_quality_ranges_and_contact_refs_exact(
    episode: EpisodeEnv, index: MediaQueryIndex
) -> None:
    overlapping = find_quality_ranges(index.connection, "edit-source-video", 5, 35)
    assert [(row.kind, row.start_frame, row.end_frame) for row in overlapping] == [
        ("black", *GOLDEN_BLACK_SPAN),
        ("blur", *GOLDEN_BLUR_SPAN),
    ]
    black = overlapping[0]
    assert black.confidence == 900
    assert black.rule_id == "black-mean-fraction-v1"
    assert black.analyzer_version == "todo35-v1"
    only_exposure = find_quality_ranges(
        index.connection, "edit-source-video", 0, 80, kind="exposure_over"
    )
    assert [(row.kind, row.start_frame, row.end_frame) for row in only_exposure] == [
        ("exposure_over", *GOLDEN_EXPOSURE_SPAN)
    ]
    assert only_exposure[0].confidence == 850

    contacts = get_contact_refs(index.connection, "edit-source-video")
    assert len(contacts) == 1
    sheet = contacts[0]
    assert sheet.frame_indexes == GOLDEN_SHEET_FRAMES
    assert (sheet.cols, sheet.rows, sheet.thumb_w, sheet.thumb_h) == GOLDEN_SHEET_LAYOUT
    assert sheet.cadence_frames == 10
    assert sheet.generator == "python-zlib-png-v1"
    assert len(sheet.sheet_sha256) == 64
    sheet_file = Path(episode.sheet_paths[0])
    assert sheet_file.is_file()
    assert sheet.sheet_path == sheet_file.name
    assert sheet.sheet_sha256 == sha256_file(sheet_file)


def test_statistics_are_int_encoded(episode: EpisodeEnv, index: MediaQueryIndex) -> None:
    stats = {row.metric: row.value for row in get_statistics(index.connection, "edit-source-audio")}
    assert stats["peak_sample"] == 16384
    assert stats["peak_mb"] == MB_SQUARE_HALF
    assert stats["clipping_count"] == 0
    assert stats["frame_count"] == 600000
    assert stats["dialogue_rms_mb"] == MB_SQUARE_HALF
    assert stats["ambient_noise_floor_mb"] == MB_FLOOR
    assert stats["dialogue_sample_count"] == 576000
    assert stats["ambient_sample_count"] == 24000
    # the loudness-summary RMS is the ebur128 filter's own measurement; the
    # index must mirror the canonical artifact exactly, not the window grid
    loudness = episode.analysis.measurements.loudness
    assert stats["rms_mean_mb"] == loudness.rms_mean_mb == -6199
    assert stats["integrated_loudness_mlufs"] == loudness.integrated_loudness_mlufs
    for row in get_statistics(index.connection, "edit-source-audio"):
        assert isinstance(row.value, int)
        assert row.unit in {"mb", "mlu", "sample", "count"}
        assert row.analyzer_version == "todo34-v1"


def test_rebuild_row_level_determinism(
    episode: EpisodeEnv, index: MediaQueryIndex, tmp_path: Path
) -> None:
    first_dump = dump_tables(index.connection)
    index.close()
    # a second episode dir rebuilt from the same store must agree ROW-level
    second_episode = tmp_path / "ep-determinism"
    second_episode.mkdir()
    second = MediaQueryIndex.open(second_episode / MEDIA_DB_NAME)
    second.rebuild(episode.store, episode.registry, second_episode)
    assert dump_tables(second.connection) == first_dump
    # rebuild in place also reproduces identical ordered row sets
    second.rebuild(episode.store, episode.registry, second_episode)
    assert dump_tables(second.connection) == first_dump
    second.close()


def test_upsert_incremental_matches_full_rebuild(
    episode: EpisodeEnv, index: MediaQueryIndex, tmp_path: Path
) -> None:
    full_dump = dump_tables(index.connection)
    index.close()
    fresh = MediaQueryIndex.open(tmp_path / "ep-upsert" / MEDIA_DB_NAME)
    for artifact in (episode.transcript, episode.analysis, episode.visual):
        fresh.upsert_analyzer_artifact(artifact)
    assert dump_tables(fresh.connection) == full_dump
    # idempotent re-upsert leaves the row sets unchanged
    fresh.upsert_analyzer_artifact(episode.analysis)
    assert dump_tables(fresh.connection) == full_dump
    fresh.close()


def test_read_only_reopen_queries_work_writes_refused(
    episode: EpisodeEnv, index: MediaQueryIndex
) -> None:
    expected = find_transcript_segments(index.connection, "edit-source-audio", 0, 12500)
    assert len(expected) == 3
    index.close()
    read_only = MediaQueryIndex.open(episode.episode_dir / MEDIA_DB_NAME, read_only=True)
    assert find_transcript_segments(read_only.connection, "edit-source-audio", 0, 12500) == expected
    assert len(find_quality_ranges(read_only.connection, "edit-source-video", 0, 80)) == 3
    with pytest.raises(duckdb.Error):
        read_only.connection.execute("DELETE FROM sources")
    with pytest.raises(duckdb.Error):
        read_only.connection.execute(
            "INSERT INTO statistics VALUES ('x', 'y', 1, 'count', 'v', 'z')"
        )
    assert len(find_transcript_segments(read_only.connection, "edit-source-audio", 0, 12500)) == 3
    read_only.close()


# -------------------------------------------------------------- failure


def test_interrupted_rebuild_transaction_rolls_back(
    episode: EpisodeEnv, index: MediaQueryIndex, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = dump_tables(index.connection)
    index.close()
    episode_copy = tmp_path / "ep-interrupt"
    episode_copy.mkdir()
    db_copy = episode_copy / MEDIA_DB_NAME
    shutil.copyfile(episode.episode_dir / MEDIA_DB_NAME, db_copy)

    original = index_module.insert_artifact_rows

    def make_exploding(
        fail_on_call: int,
    ) -> Callable[[duckdb.DuckDBPyConnection, AnalyzerArtifact, str], None]:
        calls = 0

        def exploding(
            connection: duckdb.DuckDBPyConnection,
            artifact: AnalyzerArtifact,
            artifact_sha: str,
        ) -> None:
            nonlocal calls
            calls += 1
            if calls == fail_on_call:
                raise RuntimeError(f"injected fault #{fail_on_call}")
            original(connection, artifact, artifact_sha)

        return exploding

    # repeated interruptions: a fault after each artifact still leaves the
    # previous committed state fully intact (transaction rollback)
    for fail_on_call in (1, 2):
        monkeypatch.setattr(
            index_module, "insert_artifact_rows", make_exploding(fail_on_call)
        )
        interrupted = MediaQueryIndex.open(db_copy)
        with pytest.raises(RuntimeError, match="injected fault"):
            interrupted.rebuild(episode.store, episode.registry, episode_copy)
        interrupted.close()
        check = MediaQueryIndex.open(db_copy)
        assert dump_tables(check.connection) == baseline
        check.close()

    monkeypatch.undo()
    clean = MediaQueryIndex.open(db_copy)
    clean.rebuild(episode.store, episode.registry, episode_copy)
    assert dump_tables(clean.connection) == baseline
    clean.close()


def test_stale_analyzer_version_rejected(
    episode: EpisodeEnv, tmp_path: Path
) -> None:
    stale = _build_transcript(producer_version="todo33-v9")
    fresh = MediaQueryIndex.open(tmp_path / "ep-stale" / MEDIA_DB_NAME)
    with pytest.raises(Exception, match="unsupported-analyzer-version"):
        fresh.upsert_analyzer_artifact(stale)
    assert count_scalar(fresh.connection, "SELECT count(*) FROM lineage") == 0
    fresh.close()

    payload = canonical_model_bytes(stale)
    envelope = ArtifactEnvelope(
        artifact_id=stale.artifact_id,
        artifact_type=stale.artifact_type,
        schema_version=stale.schema_version,
        content_hash=hashlib.sha256(payload).hexdigest(),
        producer=stale.producer,
        inputs=stale.inputs,
    )
    isolated_store = ArtifactStore(tmp_path / "stale-store")
    isolated_registry = ArtifactRegistry(tmp_path / "stale-registry")
    receipt = isolated_store.publish(PublicationIntent(envelope=envelope), payload)
    isolated_registry.register(isolated_store, receipt)
    stale_episode = tmp_path / "ep-stale-registry"
    stale_episode.mkdir()
    rebuilt = MediaQueryIndex.open(stale_episode / MEDIA_DB_NAME)
    with pytest.raises(Exception, match="unsupported-analyzer-version"):
        rebuilt.rebuild(isolated_store, isolated_registry, stale_episode)
    assert count_scalar(rebuilt.connection, "SELECT count(*) FROM lineage") == 0
    rebuilt.close()


def test_parameter_injection_probe_survives(index: MediaQueryIndex) -> None:
    probe = "'; DROP TABLE sources; --"
    assert find_transcript_segments(index.connection, probe, 0, 100) == ()
    assert find_silence_ranges(index.connection, probe, 0, 100) == ()
    assert find_quality_ranges(index.connection, probe, 0, 100) == ()
    assert find_quality_ranges(
        index.connection, "edit-source-video", 0, 80, kind="black' OR 1=1; --"
    ) == ()
    assert get_contact_refs(index.connection, probe) == ()
    assert get_statistics(index.connection, probe) == ()
    tables = {str(row[0]) for row in index.connection.execute("SHOW TABLES").fetchall()}
    assert set(DATA_TABLES) <= tables
    assert count_scalar(index.connection, "SELECT count(*) FROM sources") == 3


def test_missing_artifact_parent_refused(episode: EpisodeEnv, index: MediaQueryIndex) -> None:
    index.close()
    ghost_sha = episode.receipts[1].content_sha256
    object_file = (
        episode.store.store_root / "objects" / ghost_sha[:2] / ghost_sha
    )
    original = object_file.read_bytes()
    object_file.unlink()
    refused = MediaQueryIndex.open(episode.episode_dir / MEDIA_DB_NAME)
    try:
        with pytest.raises(MediaQueryError, match="object-missing"):
            refused.rebuild(episode.store, episode.registry, episode.episode_dir)
    finally:
        object_file.write_bytes(original)
        refused.close()


def test_tampered_artifact_bytes_refused(episode: EpisodeEnv, index: MediaQueryIndex) -> None:
    index.close()
    tampered_sha = episode.receipts[2].content_sha256
    object_file = (
        episode.store.store_root / "objects" / tampered_sha[:2] / tampered_sha
    )
    original = object_file.read_bytes()
    object_file.write_bytes(b'{"tampered": true}')
    refused = MediaQueryIndex.open(episode.episode_dir / MEDIA_DB_NAME)
    try:
        with pytest.raises(MediaQueryError, match="content-hash-mismatch"):
            refused.rebuild(episode.store, episode.registry, episode.episode_dir)
    finally:
        object_file.write_bytes(original)
        refused.close()


def test_corrupt_index_file_refuses_to_open(tmp_path: Path) -> None:
    corrupt = tmp_path / MEDIA_DB_NAME
    corrupt.write_bytes(b"this is not a duckdb database file")
    with pytest.raises(Exception, match="index-unreadable"):
        MediaQueryIndex.open(corrupt)
