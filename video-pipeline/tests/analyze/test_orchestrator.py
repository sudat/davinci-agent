"""Analyzer orchestration (Todo 38): cache reuse, parallelism, stale suppression.

The orchestrator wraps the EXISTING Todo 33-35 stacks behind a runner seam
(no internals are forked): it computes cache keys (edit-source world sha +
analyzer name/version + parameter hash [+ model/prompt hash]), executes
independent analyzers concurrently over read-only inputs and per-spec work
dirs, publishes immutable results through the Todo-7 store under the Todo-36
publication convention, sweeps stale outputs to SUPERSEDED (never deleted),
and rebuilds the Todo-36 DuckDB index from non-superseded evidence only.

Designed inputs (pre-registered from the synthesis parameters):

- audio 48 kHz: square tone 1750 ms / silence 500 ms / square tone 1750 ms
  with a transcript whose middle segment is that silence -> one measured
  silence span around [84000, 108000) samples;
- video: 32 CFR30 frames = black [0,8) / smptebars / blurred bars [16,24) /
  white [24,32).

Analyzer completion NEVER advances committed Plans: an AST/import-closure
guard keeps job_runner lanes/CAS/transitions/stage-runner and review_command
out of the orchestrator modules entirely.
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pytest

from services.analyze.analysis_models import AnalyzeRequestError, StreamFacts
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
from services.analyze.audio_constants import ANALYZER_VERSION as AUDIO_VERSION
from services.analyze.audio_constants import frozen_constants_hash as audio_constants_hash
from services.analyze.orchestrator import (
    FAULT_BEFORE_INDEX,
    FAULT_BEFORE_PUBLISH,
    AnalyzeOrchestrator,
)
from services.analyze.orchestrator_models import (
    AnalyzerArtifact,
    AnalyzerSpec,
    EditSourceFile,
    EditSourceRef,
    OrchestrationRecord,
    OrchestrationResult,
    OrchestratorCacheConflictError,
    edit_source_world_sha,
    orchestration_cache_key,
    spec_parameter_hash,
    verify_edit_source,
)
from services.analyze.orchestrator_state import STATE_FILE_NAME, SupersessionLedger
from services.analyze.visual_analysis import VisualAnalysisRequest, analyze_visual
from services.analyze.visual_constants import ANALYZER_VERSION as VISUAL_VERSION
from services.analyze.visual_constants import frozen_constants_hash as visual_constants_hash
from services.artifact_registry.registry import ArtifactRegistry
from services.artifact_store.store import ArtifactStore
from services.contracts.primitives import Producer
from services.foundation_io import canonical_model_bytes, sha256_file
from services.media_query.index import MEDIA_DB_NAME, MediaQueryIndex
from services.toolchain.models import Phase1TechnicalToolchainLock, load_lock

PHASE1_LOCK = Path("config/toolchains/phase-1-technical-v2.json")
SQUARE = r"if(lt(mod(t*440\,1)\,0.5)\,0.5\,-0.5)"
TRANSCRIPT_SEGMENTS = (
    (0, 1750, "今日は撮影の裏側をお見せします。"),
    (1750, 2250, ""),
    (2250, 4000, "まずカメラのセッティングからです。"),
)
# smptebars is unusable on this host: the pinned ffmpeg intermittently drops
# bytes from that filter's option string (15/15 fails vs 15/15 passes for the
# graphs below); testsrc+gblur provides the same sharp/blurred contrast.
SEGMENT_GRAPHS = (
    ("color=black:s=320x180:r=30", 8),
    ("testsrc=size=320x180:rate=30", 8),
    ("testsrc=size=320x180:rate=30,gblur=sigma=8", 8),
    ("color=white:s=320x180:r=30", 8),
)
GOLDEN_FRAME_COUNT = 32
GOLDEN_BLACK_SPAN = (0, 8)
GOLDEN_BLUR_SPAN = (16, 24)
SERVICES_ROOT = Path(__file__).resolve().parents[2] / "services"
FORBIDDEN_MODULES = {
    "services.job_runner.lanes",
    "services.job_runner.cas",
    "services.job_runner.transitions",
    "services.job_runner.stage_runner",
    "services.review_command",
    "services.review_command.commit",
}
FORBIDDEN_TOKENS = ("job_runner", "review_command")


@dataclass(frozen=True, slots=True)
class EpisodeWorld:
    """Session-scoped synthesized episode + the serial baseline artifacts."""

    root: Path
    wav: Path
    avi: Path
    transcript: TranscriptArtifact
    audio_artifact_sha: str
    visual_artifact_sha: str
    audio_spec: AnalyzerSpec
    visual_spec: AnalyzerSpec

    def source_for(self, *, wav: Path | None = None, avi: Path | None = None) -> EditSourceRef:
        audio = wav or self.wav
        video = avi or self.avi
        return EditSourceRef(
            source_id="ep-001",
            files=(
                EditSourceFile(role="audio", path=str(audio), sha256=sha256_file(audio)),
                EditSourceFile(role="video", path=str(video), sha256=sha256_file(video)),
            ),
        )

    def local_copy(self, tmp_path: Path) -> tuple[Path, Path]:
        """Private media copies for tests that mutate the edit source."""

        wav = tmp_path / "designed48.wav"
        avi = tmp_path / "designed.avi"
        wav.write_bytes(self.wav.read_bytes())
        avi.write_bytes(self.avi.read_bytes())
        return wav, avi


@dataclass(frozen=True, slots=True)
class Env:
    store: ArtifactStore
    registry: ArtifactRegistry
    episode_dir: Path
    state_root: Path
    work_root: Path

    def orchestrator(
        self, fault_hook: Callable[[str], None] | None = None
    ) -> AnalyzeOrchestrator:
        return AnalyzeOrchestrator(
            store=self.store,
            registry=self.registry,
            episode_dir=self.episode_dir,
            state_root=self.state_root,
            work_root=self.work_root,
            fault_hook=fault_hook,
        )


class RunCounter:
    """Thread-safe instrument counting runner-seam invocations by analyzer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[str] = []

    def record(self, analyzer_name: str) -> None:
        with self._lock:
            self.calls.append(analyzer_name)


class SimKillError(RuntimeError):
    """Simulated process death raised from a fault seam."""


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


def _build_transcript() -> TranscriptArtifact:
    binding = AsrInputBinding(
        media_path="/var/media/episode.m4v",
        media_sha256="b" * 64,
        wav_sha256="a" * 64,
        wav_probe=WavProbeSummary(),
    )
    parsed = tuple(
        TranscriptSegment(start_ms=start, end_ms=end, text=text)
        for start, end, text in TRANSCRIPT_SEGMENTS
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
        producer=Producer(name="asr-whisper-cpp", version="todo33-v1"),
        inputs=(),
        input_binding=binding,
        segments=parsed,
        experimental=experimental,
        settings_echo=echo,
    )


@pytest.fixture(scope="session")
def toolchain_lock() -> Phase1TechnicalToolchainLock:
    loaded = load_lock(PHASE1_LOCK)
    assert isinstance(loaded, Phase1TechnicalToolchainLock)
    if not Path(loaded.ffmpeg.ffmpeg.path).is_file() or not Path(
        loaded.ffmpeg.ffprobe.path
    ).is_file():
        pytest.skip("pinned ffmpeg/ffprobe not bootstrapped")
    return loaded


@pytest.fixture(scope="session")
def world(
    toolchain_lock: Phase1TechnicalToolchainLock, tmp_path_factory: pytest.TempPathFactory
) -> EpisodeWorld:
    """Tiny designed episode plus the serial (direct stack) baseline run."""

    root = tmp_path_factory.mktemp("todo38-world")
    ffmpeg = Path(toolchain_lock.ffmpeg.ffmpeg.path)

    wav = root / "designed48.wav"
    _run_ffmpeg(
        ffmpeg,
        (
            "-f", "lavfi", "-i", f"aevalsrc=exprs={SQUARE}:s=48000:d=1.75",
            "-f", "lavfi", "-i", "aevalsrc=exprs=0:s=48000:d=0.5",
            "-f", "lavfi", "-i", f"aevalsrc=exprs={SQUARE}:s=48000:d=1.75",
            "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1",
            "-c:a", "pcm_s16le",
        ),
        wav,
    )
    with wave.open(str(wav)) as stream:
        assert stream.getnframes() == 192000

    segments = []
    for index, (graph, frames) in enumerate(SEGMENT_GRAPHS):
        segment = root / f"vseg{index}.avi"
        _run_ffmpeg(
            ffmpeg,
            ("-f", "lavfi", "-i", graph, "-frames:v", str(frames), "-c:v", "ffv1"),
            segment,
        )
        segments.append(segment)
    chain = "".join(f"[{index}:v]" for index in range(len(segments)))
    avi = root / "designed.avi"
    _run_ffmpeg(
        ffmpeg,
        (
            *[part for segment in segments for part in ("-i", str(segment))],
            "-filter_complex", f"{chain}concat=n={len(segments)}:v=1:a=0[v]",
            "-map", "[v]", "-c:v", "ffv1",
        ),
        avi,
    )

    transcript = _build_transcript()
    audio = analyze_dialogue(
        AudioAnalysisRequest(
            wav_path=str(wav),
            wav_sha256=sha256_file(wav),
            declared=StreamFacts(sample_rate=48000, channels=1, codec="pcm_s16le"),
            transcript=transcript,
        ),
        work_dir=root / "audio-work",
        cache_dir=root / "audio-cache",
    ).artifact
    assert audio.producer.name == "analyze-dialogue"
    visual = analyze_visual(
        VisualAnalysisRequest(
            media_path=str(avi), media_sha256=sha256_file(avi), fixture_only=True
        ),
        work_dir=root / "visual-work",
    ).artifact
    assert visual.producer.name == "analyze-visual"
    assert visual.media.facts.frame_count == GOLDEN_FRAME_COUNT
    assert any(
        (span.span.start_frame, span.span.end_frame) == GOLDEN_BLACK_SPAN
        for span in visual.black_spans
    )
    assert any(
        (span.span.start_frame, span.span.end_frame) == GOLDEN_BLUR_SPAN
        for span in visual.blur_spans
    )

    audio_spec = AnalyzerSpec(
        analyzer_name="analyze-dialogue",
        analyzer_version=AUDIO_VERSION,
        parameter_hash=spec_parameter_hash(
            {
                "transcript_sha256": transcript.content_hash,
                "frozen_constants_sha256": audio_constants_hash(),
            }
        ),
    )
    visual_spec = AnalyzerSpec(
        analyzer_name="analyze-visual",
        analyzer_version=VISUAL_VERSION,
        parameter_hash=spec_parameter_hash(
            {"frozen_constants_sha256": visual_constants_hash()}
        ),
    )
    return EpisodeWorld(
        root=root,
        wav=wav,
        avi=avi,
        transcript=transcript,
        audio_artifact_sha=hashlib.sha256(canonical_model_bytes(audio)).hexdigest(),
        visual_artifact_sha=hashlib.sha256(canonical_model_bytes(visual)).hexdigest(),
        audio_spec=audio_spec,
        visual_spec=visual_spec,
    )


def make_env(tmp_path: Path) -> Env:
    episode_dir = tmp_path / "jobs" / "ep-001"
    episode_dir.mkdir(parents=True)
    return Env(
        store=ArtifactStore(tmp_path / "store"),
        registry=ArtifactRegistry(tmp_path / "registry"),
        episode_dir=episode_dir,
        state_root=tmp_path / "orchestration",
        work_root=tmp_path / "work",
    )


def make_runner(
    world: EpisodeWorld,
    *,
    counter: RunCounter | None = None,
    barrier: threading.Barrier | None = None,
    fail_names: frozenset[str] = frozenset(),
) -> Callable[[AnalyzerSpec, EditSourceRef, Path], AnalyzerArtifact]:
    def file_by_role(source: EditSourceRef, role: str) -> EditSourceFile:
        return next(item for item in source.files if item.role == role)

    def analyze(spec: AnalyzerSpec, source: EditSourceRef, work_dir: Path) -> AnalyzerArtifact:
        if counter is not None:
            counter.record(spec.analyzer_name)
        if spec.analyzer_name in fail_names:
            raise AnalyzeRequestError(f"injected failure for {spec.analyzer_name}")
        if spec.analyzer_name == "analyze-dialogue":
            audio_file = file_by_role(source, "audio")
            return analyze_dialogue(
                AudioAnalysisRequest(
                    wav_path=audio_file.path,
                    wav_sha256=audio_file.sha256,
                    declared=StreamFacts(sample_rate=48000, channels=1, codec="pcm_s16le"),
                    transcript=world.transcript,
                ),
                work_dir=work_dir / "audio",
                cache_dir=work_dir / "cache",
            ).artifact
        if spec.analyzer_name == "analyze-visual":
            video_file = file_by_role(source, "video")
            return analyze_visual(
                VisualAnalysisRequest(
                    media_path=video_file.path,
                    media_sha256=video_file.sha256,
                    fixture_only=True,
                ),
                work_dir=work_dir / "visual",
            ).artifact
        raise AnalyzeRequestError(f"unknown analyzer {spec.analyzer_name}")

    def runner(spec: AnalyzerSpec, source: EditSourceRef, work_dir: Path) -> AnalyzerArtifact:
        artifact = analyze(spec, source, work_dir)
        if barrier is not None:
            barrier.wait(timeout=60)  # rendezvous proves genuine overlap
        return artifact

    return runner


def lineage_shas(env: Env) -> set[str]:
    index = MediaQueryIndex.open(env.episode_dir / MEDIA_DB_NAME, read_only=True)
    try:
        rows = index.connection.execute(
            "SELECT artifact_sha FROM lineage ORDER BY artifact_sha"
        ).fetchall()
    finally:
        index.close()
    return {str(row[0]) for row in rows}


def dump_lineage(env: Env) -> list[tuple[object, ...]]:
    index = MediaQueryIndex.open(env.episode_dir / MEDIA_DB_NAME, read_only=True)
    try:
        rows = index.connection.execute(
            "SELECT * FROM lineage ORDER BY artifact_sha"
        ).fetchall()
    finally:
        index.close()
    return [tuple(row) for row in rows]


def store_object_count(env: Env) -> int:
    return sum(1 for _ in (env.store.store_root / "objects").glob("*/*"))


def scalar(connection: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = connection.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def artifact_shas(record: OrchestrationRecord) -> set[str]:
    shas: set[str] = set()
    for item in record.results:
        assert item.artifact is not None
        shas.add(item.artifact.sha256)
    return shas


def result_by_name(record: OrchestrationRecord, name: str) -> OrchestrationResult:
    return next(item for item in record.results if item.spec.analyzer_name == name)


def read_state_bindings(env: Env) -> dict[str, dict[str, object]]:
    raw = json.loads((env.state_root / STATE_FILE_NAME).read_text())
    bindings = raw["bindings"]
    assert isinstance(bindings, dict)
    return bindings


# ============================================================== unit: keys


def test_cache_key_and_world_sha_are_deterministic_and_sensitive(
    world: EpisodeWorld,
) -> None:
    source = world.source_for()
    world_sha = edit_source_world_sha(source)
    base_key = orchestration_cache_key(edit_source_sha256=world_sha, spec=world.audio_spec)
    assert base_key == orchestration_cache_key(
        edit_source_sha256=world_sha, spec=world.audio_spec
    )
    # every cache-key input is sensitive: world, version, params, prompt hash
    assert (
        orchestration_cache_key(edit_source_sha256="0" * 64, spec=world.audio_spec)
        != base_key
    )
    assert (
        orchestration_cache_key(
            edit_source_sha256=world_sha,
            spec=world.audio_spec.model_copy(update={"analyzer_version": "todo34-v2"}),
        )
        != base_key
    )
    assert (
        orchestration_cache_key(
            edit_source_sha256=world_sha,
            spec=world.audio_spec.model_copy(update={"parameter_hash": "0" * 64}),
        )
        != base_key
    )
    with_prompt = world.audio_spec.model_copy(update={"model_prompt_hash": "1" * 64})
    assert (
        orchestration_cache_key(edit_source_sha256=world_sha, spec=with_prompt) != base_key
    )
    # None and "" are the same absence of a model/prompt hash (frozen rule)
    assert (
        orchestration_cache_key(
            edit_source_sha256=world_sha,
            spec=with_prompt.model_copy(update={"model_prompt_hash": None}),
        )
        == base_key
    )
    # world sha: any file hash change changes the world
    assert verify_edit_source(source) == world_sha
    drifted = source.model_copy(
        update={
            "files": (
                EditSourceFile(role="audio", path=str(world.wav), sha256="c" * 64),
                source.files[1],
            )
        }
    )
    assert edit_source_world_sha(drifted) != world_sha
    with pytest.raises(Exception, match="edit-source-drift"):
        verify_edit_source(drifted)
    # parameter hash is canonical: insertion order cannot matter
    assert spec_parameter_hash({"a": 1, "b": ("x", "y")}) == spec_parameter_hash(
        {"b": ("x", "y"), "a": 1}
    )


# ================================================================== happy


def test_happy_two_analyzers_publish_and_rebuild_index(
    world: EpisodeWorld, tmp_path: Path
) -> None:
    env = make_env(tmp_path)
    record = env.orchestrator().run_analyzers(
        world.source_for(), (world.audio_spec, world.visual_spec), make_runner(world)
    )
    assert record.failures == ()
    assert len(record.results) == 2
    audio_result = result_by_name(record, "analyze-dialogue")
    visual_result = result_by_name(record, "analyze-visual")
    assert audio_result.cache_status == "miss"
    assert visual_result.cache_status == "miss"
    assert audio_result.superseded is False
    assert visual_result.superseded is False
    assert audio_result.artifact is not None
    assert visual_result.artifact is not None
    assert audio_result.artifact.sha256 == world.audio_artifact_sha
    assert visual_result.artifact.sha256 == world.visual_artifact_sha

    assert store_object_count(env) == 2
    assert len(env.registry.load().entries) == 2
    assert lineage_shas(env) == {world.audio_artifact_sha, world.visual_artifact_sha}

    index = MediaQueryIndex.open(env.episode_dir / MEDIA_DB_NAME, read_only=True)
    try:
        connection = index.connection
        assert scalar(connection, "SELECT count(*) FROM silence_ranges") > 0
        assert scalar(connection, "SELECT count(*) FROM quality_ranges") > 0
        assert scalar(connection, "SELECT count(*) FROM contact_refs") == 1
        assert scalar(connection, "SELECT count(*) FROM sources") == 2
    finally:
        index.close()
    assert record.superseded_artifacts == ()


def test_second_identical_run_is_all_cache_hits(world: EpisodeWorld, tmp_path: Path) -> None:
    env = make_env(tmp_path)
    orchestrator = env.orchestrator()
    first = orchestrator.run_analyzers(
        world.source_for(), (world.audio_spec, world.visual_spec), make_runner(world)
    )
    counter = RunCounter()
    second = orchestrator.run_analyzers(
        world.source_for(),
        (world.audio_spec, world.visual_spec),
        make_runner(world, counter=counter),
    )
    assert counter.calls == []  # no re-execution at all
    assert all(item.cache_status == "hit" for item in second.results)
    assert all(item.superseded is False for item in second.results)
    assert artifact_shas(second) == artifact_shas(first)
    assert store_object_count(env) == 2  # byte-identical: nothing new published
    assert len(env.registry.load().entries) == 2
    assert lineage_shas(env) == {world.audio_artifact_sha, world.visual_artifact_sha}


def test_parallel_run_overlaps_and_matches_serial(world: EpisodeWorld, tmp_path: Path) -> None:
    env = make_env(tmp_path)
    barrier = threading.Barrier(2)
    record = env.orchestrator().run_analyzers(
        world.source_for(),
        (world.audio_spec, world.visual_spec),
        make_runner(world, barrier=barrier),
    )
    # both workers reached the rendezvous: execution genuinely overlapped
    assert record.failures == ()
    assert artifact_shas(record) == {world.audio_artifact_sha, world.visual_artifact_sha}
    assert not barrier.broken


# ======================================================== failure: parents


def test_parent_change_mid_run_supersedes_not_adopts(world: EpisodeWorld, tmp_path: Path) -> None:
    env = make_env(tmp_path)
    wav, avi = world.local_copy(tmp_path)
    source = world.source_for(wav=wav, avi=avi)
    publish_calls: list[int] = []

    def swap_hook(point: str) -> None:
        if point != FAULT_BEFORE_PUBLISH:
            return
        publish_calls.append(len(publish_calls) + 1)
        if len(publish_calls) == 2:
            # the second analyzer completed; swap the edit-source bytes
            # before its publish: its parent world is now stale
            with wav.open("ab") as stream:
                stream.write(b"\x00" * 16)

    record = env.orchestrator(fault_hook=swap_hook).run_analyzers(
        source, (world.audio_spec, world.visual_spec), make_runner(world)
    )
    audio_result = result_by_name(record, "analyze-dialogue")
    visual_result = result_by_name(record, "analyze-visual")
    assert audio_result.artifact is not None  # adopted before the swap
    assert audio_result.superseded is False
    assert visual_result.superseded is True  # stale parent: not adopted
    assert visual_result.artifact is None
    assert store_object_count(env) == 1
    assert lineage_shas(env) == {audio_result.artifact.sha256}
    # only the adopted analyzer left a cache binding
    assert len(read_state_bindings(env)) == 1


def test_subsequent_run_supersedes_stale_world_outputs(world: EpisodeWorld, tmp_path: Path) -> None:
    env = make_env(tmp_path)
    orchestrator = env.orchestrator()
    first = orchestrator.run_analyzers(
        world.source_for(), (world.audio_spec, world.visual_spec), make_runner(world)
    )
    assert first.superseded_artifacts == ()
    old_audio = result_by_name(first, "analyze-dialogue").artifact
    old_visual = result_by_name(first, "analyze-visual").artifact
    assert old_audio is not None
    assert old_visual is not None

    # a NEW committed edit source: same episode, changed audio bytes
    wav2 = tmp_path / "designed48-v2.wav"
    wav2.write_bytes(world.wav.read_bytes() + b"\x00" * 16)
    second = orchestrator.run_analyzers(
        world.source_for(wav=wav2), (world.audio_spec,), make_runner(world)
    )
    new_audio = result_by_name(second, "analyze-dialogue").artifact
    assert new_audio is not None
    assert new_audio.sha256 != old_audio.sha256
    assert {ref.artifact_id for ref in second.superseded_artifacts} == {
        old_audio.artifact_id,
        old_visual.artifact_id,
    }
    # NEVER deleted: registry + store keep every artifact as audit history
    assert set(env.registry.load().entries) == {
        old_audio.artifact_id,
        old_visual.artifact_id,
        new_audio.artifact_id,
    }
    assert store_object_count(env) == 3
    ledger = SupersessionLedger(env.registry.index_root)
    assert set(ledger.entries()) == {old_audio.artifact_id, old_visual.artifact_id}
    # fresh index rebuild excludes superseded rows: only the new audio is live
    assert lineage_shas(env) == {new_audio.sha256}
    index = MediaQueryIndex.open(env.episode_dir / MEDIA_DB_NAME, read_only=True)
    try:
        assert scalar(index.connection, "SELECT count(*) FROM quality_ranges") == 0
    finally:
        index.close()


# ========================================================= failure: cache


def test_conflicting_cache_binding_raises_explicit_error(
    world: EpisodeWorld, tmp_path: Path
) -> None:
    env = make_env(tmp_path)
    orchestrator = env.orchestrator()
    orchestrator.run_analyzers(
        world.source_for(), (world.audio_spec, world.visual_spec), make_runner(world)
    )
    baseline_lineage = dump_lineage(env)

    # tamper one binding: the key now points at foreign content
    state_path = env.state_root / STATE_FILE_NAME
    raw = json.loads(state_path.read_text())
    audio_key = next(
        key
        for key, binding in raw["bindings"].items()
        if isinstance(binding, dict) and binding["spec"]["analyzer_name"] == "analyze-dialogue"
    )
    raw["bindings"][audio_key]["content_sha256"] = "0" * 64
    state_path.write_text(json.dumps(raw))

    counter = RunCounter()
    with pytest.raises(OrchestratorCacheConflictError, match="orchestrator_cache_conflict"):
        orchestrator.run_analyzers(
            world.source_for(),
            (world.audio_spec, world.visual_spec),
            make_runner(world, counter=counter),
        )
    # refused before any execution or adoption; nothing silently reused
    assert counter.calls == []
    assert store_object_count(env) == 2
    assert len(env.registry.load().entries) == 2
    assert dump_lineage(env) == baseline_lineage


# ====================================================== failure: isolation


def test_analyzer_failure_is_isolated(world: EpisodeWorld, tmp_path: Path) -> None:
    env = make_env(tmp_path)
    record = env.orchestrator().run_analyzers(
        world.source_for(),
        (world.visual_spec, world.audio_spec),
        make_runner(world, fail_names=frozenset({"analyze-dialogue"})),
    )
    assert len(record.failures) == 1
    assert record.failures[0].spec.analyzer_name == "analyze-dialogue"
    assert record.failures[0].error_label == "analyze_request_refused"
    visual_result = result_by_name(record, "analyze-visual")
    assert visual_result.artifact is not None
    assert record.superseded_artifacts == ()
    # the index is rebuilt only from successes
    assert lineage_shas(env) == {world.visual_artifact_sha}
    assert store_object_count(env) == 1

    # retry with a healthy runner: the failed analyzer now publishes and the
    # previously published visual evidence is NOT superseded (no drift)
    recovered = env.orchestrator().run_analyzers(
        world.source_for(), (world.visual_spec, world.audio_spec), make_runner(world)
    )
    assert recovered.failures == ()
    assert recovered.superseded_artifacts == ()
    assert lineage_shas(env) == {world.visual_artifact_sha, world.audio_artifact_sha}


def test_duplicate_completion_publishes_once(world: EpisodeWorld, tmp_path: Path) -> None:
    env = make_env(tmp_path)
    counter = RunCounter()
    record = env.orchestrator().run_analyzers(
        world.source_for(),
        (world.audio_spec, world.audio_spec),
        make_runner(world, counter=counter),
    )
    assert counter.calls == ["analyze-dialogue", "analyze-dialogue"]
    assert len(record.results) == 2
    first, second = record.results
    assert first.artifact is not None
    assert second.artifact is not None
    assert first.artifact == second.artifact
    assert first.publish_idempotent is False
    assert second.publish_idempotent is True  # store idempotency: one publication
    assert store_object_count(env) == 1
    assert len(env.registry.load().entries) == 1
    assert lineage_shas(env) == {world.audio_artifact_sha}


# ========================================================= kill / restart


def test_kill_before_publish_restart_hits_published(world: EpisodeWorld, tmp_path: Path) -> None:
    env = make_env(tmp_path)
    publish_calls: list[int] = []

    def kill_hook(point: str) -> None:
        if point == FAULT_BEFORE_PUBLISH:
            publish_calls.append(len(publish_calls) + 1)
            if len(publish_calls) == 2:  # audio published; die before visual publish
                raise SimKillError("simulated crash before publish")

    with pytest.raises(SimKillError):
        env.orchestrator(fault_hook=kill_hook).run_analyzers(
            world.source_for(), (world.audio_spec, world.visual_spec), make_runner(world)
        )
    # partial state: audio published+bound; no index yet
    assert store_object_count(env) == 1
    assert len(read_state_bindings(env)) == 1
    assert not (env.episode_dir / MEDIA_DB_NAME).exists()

    counter = RunCounter()
    record = env.orchestrator().run_analyzers(
        world.source_for(),
        (world.audio_spec, world.visual_spec),
        make_runner(world, counter=counter),
    )
    # the published spec is a byte-verified HIT; only the unfinished one re-ran
    assert counter.calls == ["analyze-visual"]
    audio_result = result_by_name(record, "analyze-dialogue")
    visual_result = result_by_name(record, "analyze-visual")
    assert audio_result.cache_status == "hit"
    assert audio_result.artifact is not None
    assert audio_result.artifact.sha256 == world.audio_artifact_sha
    assert visual_result.cache_status == "miss"
    assert lineage_shas(env) == {world.audio_artifact_sha, world.visual_artifact_sha}


def test_kill_before_index_restarts_to_identical_rows(
    world: EpisodeWorld, tmp_path: Path
) -> None:
    env = make_env(tmp_path)

    def kill_hook(point: str) -> None:
        if point == FAULT_BEFORE_INDEX:
            raise SimKillError("simulated crash before index rebuild")

    with pytest.raises(SimKillError):
        env.orchestrator(fault_hook=kill_hook).run_analyzers(
            world.source_for(), (world.audio_spec, world.visual_spec), make_runner(world)
        )
    # both analyzers completed AND published; only the index rebuild is missing
    assert store_object_count(env) == 2
    assert len(read_state_bindings(env)) == 2
    assert not (env.episode_dir / MEDIA_DB_NAME).exists()

    counter = RunCounter()
    plain = env.orchestrator()
    record = plain.run_analyzers(
        world.source_for(),
        (world.audio_spec, world.visual_spec),
        make_runner(world, counter=counter),
    )
    assert counter.calls == []  # everything already published: pure cache hits
    assert all(item.cache_status == "hit" for item in record.results)
    assert lineage_shas(env) == {world.audio_artifact_sha, world.visual_artifact_sha}

    # index rebuild is idempotent: a third run reproduces identical rows
    rows_before = dump_lineage(env)
    plain.run_analyzers(
        world.source_for(), (world.audio_spec, world.visual_spec), make_runner(world)
    )
    assert dump_lineage(env) == rows_before


# ==================================================== plan-boundary guard


def _module_file(name: str) -> Path | None:
    candidate = SERVICES_ROOT / Path(*name.split(".")).with_suffix(".py")
    return candidate if candidate.is_file() else None


def _service_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return {name for name in found if name.startswith("services")}


def test_orchestrator_modules_never_import_plan_mutation_paths() -> None:
    sources = sorted(SERVICES_ROOT.joinpath("analyze").glob("orchestrator*.py"))
    assert len(sources) >= 2
    # grep guard: the forbidden subsystems are not even mentioned
    for source in sources:
        text = source.read_text()
        for token in FORBIDDEN_TOKENS:
            assert token not in text, f"{source.name} mentions {token}"
    # import-closure guard: no transitive dependency may reach them either
    seen: set[str] = set()
    stack = [f"services.analyze.{path.stem}" for path in sources]
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        file = _module_file(module)
        if file is None:
            continue
        stack.extend(_service_imports(file) - seen)
    offenders = seen & FORBIDDEN_MODULES
    assert not offenders, f"plan-mutation modules reachable from orchestrator: {offenders}"


def test_lineage_rows_cover_every_data_row(world: EpisodeWorld, tmp_path: Path) -> None:
    """Lineage integrity: every data row resolves to a live lineage parent."""
    env = make_env(tmp_path)
    env.orchestrator().run_analyzers(
        world.source_for(), (world.audio_spec, world.visual_spec), make_runner(world)
    )
    index = MediaQueryIndex.open(env.episode_dir / MEDIA_DB_NAME, read_only=True)
    try:
        connection = index.connection
        for table in ("sources", "silence_ranges", "quality_ranges", "contact_refs"):
            # table names come from the local tuple constant, never callers
            orphans = scalar(
                connection,
                f"SELECT count(*) FROM {table} "  # noqa: S608
                "WHERE artifact_sha NOT IN (SELECT artifact_sha FROM lineage)",
            )
            assert orphans == 0, table
    finally:
        index.close()
