"""Real Moment Deep Review providers (plan task 4) — Tier A.

Proves the REAL path end to end with a fake http transport and a tiny REAL
media fixture: ``execute_real_review`` produces a ``MomentDeepReviewV1``
with non-synthetic lineage and ``file://`` frame refs whose PNG files really
exist on disk for the reviewed window; the deterministic review id stays
provider-independent; the assessment call carries the pinned prompt plus the
evidence payload (refs + transcript texts, never embedded images); malformed
assessment responses and transport failures are typed errors (never a fake
assessment); window/bounds violations still surface through the existing
``record_review`` rules; and ``require_real_lineage`` rejects
synthetic-lineage reviews (both the bare ``"synthetic"`` marker and the
``"synthetic-local"`` convention stamped by the existing synthetic
providers) while accepting real ones.

Fixture provenance (recorded choice): no suitable real media exists under
``tests/fixtures/`` (manifests/evidence JSON only), so the clip is GENERATED
at test setup with the PINNED phase-1 ffmpeg. The task suggested lavfi
``testsrc=duration=2:size=320x240:rate=15`` (mp4); the pinned build enables
ONLY ``testsrc2`` among testsrc filters (lock ``configure_argv``
``--enable-filter=testsrc2,...``) and its mp4 encode path is
h264_videotoolbox, so the fixture uses ``testsrc2`` at the same
geometry/rate/duration. It is real media, really decoded through the
production decode path (``bind_decode``).

Static decoupling: the module must not import ``services.editorial_v2`` (T3
lane) and must contain no network imports (transport is injected).
"""

from __future__ import annotations

import inspect
import json
import re
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest

from services.analyze.audio_probe import PinnedAudioTools, resolve_audio_tools
from services.analyze.visual_decode import probe_video_facts
from services.foundation_io import sha256_file
from services.media_intelligence import moment_review_real as real_module
from services.media_intelligence.models import (
    AudioMeasurements,
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
from services.media_intelligence.moment_review import (
    NeighboringContext,
    ReviewExecutionContext,
    ReviewLineage,
    ReviewProviders,
    ReviewWindow,
    SyntheticAudioContext,
    SyntheticFrameExtractor,
    SyntheticTranscriptLookup,
    TranscriptRef,
    WindowOutOfBoundsError,
    derive_moment_review_id,
    execute_review,
)
from services.media_intelligence.moment_review_real import (
    ASSESSMENT_TOOL,
    PROMPT_MOMENT_REVIEW,
    AssessmentBadResponseError,
    AssessmentEvidenceBundle,
    AssessmentPinSummary,
    AssessmentTransportConfig,
    AssessmentTransportError,
    MomentReviewRealError,
    RealAudioContext,
    RealFrameExtractor,
    RealReviewProviders,
    RealReviewSetup,
    RealTranscriptLookup,
    SyntheticLineageError,
    TranscriptSegmentText,
    build_assessment_call,
    build_assessment_call_codex,
    execute_real_review,
    is_synthetic_provider,
    require_real_lineage,
)
from services.media_query.index_v2 import build_index
from services.media_query.query_v2 import MediaQueryApiV2

EPISODE_ID = "ep-real-01"
SOURCE_ID = "src-cam-a"
FRAME_COUNT: Final = 30  # 2 s @ 15 fps
WINDOW: Final = ReviewWindow(start_frame=6, end_frame=24)
ENDPOINT: Final = "https://models.invalid/v1/responses"
PIN: Final = AssessmentPinSummary(provider="openai", model_id="gpt-5.6-sol")
BRIEF: Final = "Tech vlog: widget teardown walkthrough"

CANNED_RESPONSE: Final = {
    "assessment": {
        "subject_action_evolution": "colour bars sweep left to right across the window",
        "reaction_notes": "no reaction signal visible in the sampled frames",
        "timing_notes": "pattern motion is steadiest through the middle of the window",
        "best_sub_span": {"start_frame": 10, "end_frame": 20},
        "keep_rationale_candidates": ["continuous pattern motion covers the beat"],
        "remove_rationale_candidates": ["edge frames repeat the neighbouring shots"],
        "cut_in_handle": "cut in just after the sweep starts",
        "cut_out_handle": "cut out on the settle",
    },
    "confidence": {
        "overall": 0.7,
        "subject_action_evolution": 0.7,
        "reaction_notes": 0.6,
        "timing_notes": 0.6,
        "best_sub_span": 0.7,
    },
    "cost": 0.0125,
}


# ------------------------------------------------------------ fixtures


@pytest.fixture(scope="session")
def pinned_tools() -> PinnedAudioTools:
    try:
        return resolve_audio_tools()
    except (OSError, ValueError) as error:
        pytest.skip(f"pinned phase-1 ffmpeg not bootstrapped: {error}")


@pytest.fixture(scope="session")
def real_media(pinned_tools: PinnedAudioTools, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Real mp4 fixture: lavfi testsrc2 2 s 320x240 @15fps, h264_videotoolbox."""

    media = tmp_path_factory.mktemp("moment-review-real") / "clip.mp4"
    result = subprocess.run(
        (
            str(pinned_tools.ffmpeg),
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=15:duration=2",
            "-c:v",
            "h264_videotoolbox",
            "-pix_fmt",
            "yuv420p",
            str(media),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-800:]
    facts = probe_video_facts(pinned_tools.ffprobe, media)
    assert (facts.width, facts.height) == (320, 240)
    assert facts.frame_count == FRAME_COUNT
    assert (facts.rate_num, facts.rate_den) == (15, 1)
    return media


def _shot(shot_id: str, start: int, end: int) -> Shot:
    return Shot(
        shot_id=shot_id,
        source_span=EditSourceSpan(start_frame=start, end_frame=end),
        description=f"real-ish shot {shot_id}",
        visual=ShotVisual(shot_size="medium", camera_motion="static"),
        editorial=ShotEditorial(
            role="talking_head",
            select_potential="medium",
            best_moment=ShotBestMoment(frame=(start + end) // 2, why="fixture"),
            pacing="moderate",
            cuttability=ShotCuttability.model_validate({"in": "clean", "out": "clean"}),
        ),
        confidence=ShotConfidence(editorial="medium", visual="medium"),
        transcript_segments=(
            TranscriptSegment(
                segment_id=f"tr-{shot_id}-1",
                text=f"segment of {shot_id}",
                start_frame=start,
                end_frame=start + 6,
            ),
            TranscriptSegment(
                segment_id=f"tr-{shot_id}-2",
                text=f"tail of {shot_id}",
                start_frame=start + 10,
                end_frame=end,
            ),
        ),
        audio_measurements=AudioMeasurements(
            loudness_db=-20.0 if shot_id == "shot-a" else -16.0,
            energy=0.3 if shot_id == "shot-a" else 0.55,
            ambient_type="speech",
        ),
    )


@pytest.fixture
def api(tmp_path: Path) -> Iterator[MediaQueryApiV2]:
    artifact = MediaIntelligenceArtifact(
        episode_id=EPISODE_ID,
        sources=(MediaSource(source_id=SOURCE_ID, duration_frames=FRAME_COUNT),),
        shots=(_shot("shot-a", 0, 15), _shot("shot-b", 15, FRAME_COUNT)),
    )
    index = build_index(artifact, tmp_path / "v2.duckdb")
    with MediaQueryApiV2.open(index) as opened:
        yield opened


# ------------------------------------------------------------ fake transport


@dataclass(slots=True)
class FakeHttpTransport:
    response: bytes
    calls: list[tuple[str, dict[str, str], bytes, float]] = field(default_factory=list)

    def __call__(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout_s: float
    ) -> bytes:
        self.calls.append((url, dict(headers), body, timeout_s))
        return self.response


@dataclass(slots=True)
class ExplodingTransport:
    def __call__(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout_s: float
    ) -> bytes:
        raise ConnectionError("transport down")


def _real_setup(
    api: MediaQueryApiV2, media: Path, analysis_dir: Path
) -> tuple[RealReviewSetup, FakeHttpTransport]:
    transport = FakeHttpTransport(response=json.dumps(CANNED_RESPONSE).encode())
    setup = RealReviewSetup(
        providers=RealReviewProviders(
            frames=RealFrameExtractor(
                media_path=media,
                media_sha256=sha256_file(media),
                analysis_dir=analysis_dir,
                density=4,
            ),
            transcripts=RealTranscriptLookup(api=api, source_id=SOURCE_ID),
            audio=RealAudioContext(api=api),
        ),
        assessment=build_assessment_call(
            PIN, transport, AssessmentTransportConfig(endpoint=ENDPOINT)
        ),
        pin=PIN,
        brief_context=BRIEF,
    )
    return setup, transport


def _context() -> ReviewExecutionContext:
    return ReviewExecutionContext(
        episode_id=EPISODE_ID,
        source_duration_frames=FRAME_COUNT,
        known_shot_ids=frozenset({"shot-a", "shot-b"}),
        neighbors=NeighboringContext(prev_shot_id="shot-a", next_shot_id="shot-b"),
    )


# ------------------------------------------------------------ happy path


def test_real_round_produces_real_lineage_and_file_refs(
    api: MediaQueryApiV2, real_media: Path, tmp_path: Path
) -> None:
    setup, _transport = _real_setup(api, real_media, tmp_path)
    review = execute_real_review(WINDOW, setup, context=_context())

    assert review.lineage.provider == PIN.provider
    assert review.lineage.provider != "synthetic"
    assert review.lineage.provider_version == PIN.model_id
    assert review.lineage.tool == ASSESSMENT_TOOL == "multimodal-v1"
    assert review.lineage.cost == pytest.approx(0.0125)
    assert review.review_id == derive_moment_review_id(EPISODE_ID, 6, 24)

    entries = review.evidence.frame_bundle
    assert len(entries) == 4  # bounded density: min(4, span=18)
    for entry in entries:
        assert 6 <= entry.frame < 24
        assert entry.ref.startswith("file://")
        frame_path = Path(entry.ref.removeprefix("file://"))
        assert frame_path.is_file(), f"frame file missing: {frame_path}"
        assert frame_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    # half-open overlap: [0,6) touching the start and [25,30) beyond the end
    # are excluded; only genuinely overlapping segments count
    assert review.evidence.transcript_refs == ("tr-shot-a-2", "tr-shot-b-1")
    audio = review.evidence.audio_context
    assert audio.ambient_type == "speech"
    assert audio.loudness_db == pytest.approx(-16.0)  # loudest overlapping row
    assert audio.energy == pytest.approx((0.3 + 0.55) / 2)
    assert audio.note is not None
    assert "2 audio energy rows" in audio.note

    assert review.assessment.best_sub_span.start_frame == 10
    assert review.confidence.overall == pytest.approx(0.7)


def test_assessment_call_carries_prompt_and_evidence_payload(
    api: MediaQueryApiV2, real_media: Path, tmp_path: Path
) -> None:
    setup, transport = _real_setup(api, real_media, tmp_path)
    review = execute_real_review(WINDOW, setup, context=_context())

    assert len(transport.calls) == 1
    url, _headers, body, timeout_s = transport.calls[0]
    assert url == ENDPOINT
    assert timeout_s == 120.0
    payload = json.loads(body)
    assert payload["prompt"] == PROMPT_MOMENT_REVIEW
    evidence = payload["evidence"]
    assert evidence["episode_id"] == EPISODE_ID
    assert evidence["window"] == {"start_frame": 6, "end_frame": 24}
    assert evidence["frame_refs"] == [entry.ref for entry in review.evidence.frame_bundle]
    assert all(ref.startswith("file://") for ref in evidence["frame_refs"])
    assert {segment["text"] for segment in evidence["transcript_segments"]} == {
        "tail of shot-a",
        "segment of shot-b",
    }
    assert evidence["audio_note"] == review.evidence.audio_context.note
    assert evidence["brief_context"] == BRIEF
    assert set(payload["structured_output"]) == {"assessment", "confidence"}
    assert b"\x89PNG" not in body  # no embedded image bytes: refs only


# ------------------------------------------------- synthetic-lineage gate


def _synthetic_review(provider: str):
    providers = ReviewProviders(
        frames=SyntheticFrameExtractor(density=4),
        transcripts=SyntheticTranscriptLookup(
            segments=(TranscriptRef(segment_id="tr-x", start_frame=100, end_frame=200),)
        ),
        audio=SyntheticAudioContext(),
        lineage=ReviewLineage(provider=provider, provider_version="1", tool="synthetic"),
    )
    return execute_review(
        ReviewWindow(start_frame=100, end_frame=200),
        providers,
        context=ReviewExecutionContext(episode_id=EPISODE_ID, source_duration_frames=300),
    )


def test_require_real_lineage_rejects_synthetic_reviews(
    api: MediaQueryApiV2, real_media: Path, tmp_path: Path
) -> None:
    real_review = execute_real_review(
        WINDOW, _real_setup(api, real_media, tmp_path)[0], context=_context()
    )
    require_real_lineage((real_review,))  # real passes silently

    for provider in ("synthetic", "synthetic-local"):
        assert is_synthetic_provider(provider)
        stale = _synthetic_review(provider)
        with pytest.raises(SyntheticLineageError, match=stale.review_id):
            require_real_lineage((real_review, stale))
        with pytest.raises(SyntheticLineageError):
            require_real_lineage((stale,))

    assert not is_synthetic_provider("openai")
    assert not is_synthetic_provider("syntheticity")  # prefix must not over-match


# ------------------------------------------------ typed adversarial failures


def _bundle() -> AssessmentEvidenceBundle:
    """Minimal valid bundle; the fake transports never read the frame ref."""

    return AssessmentEvidenceBundle(
        episode_id=EPISODE_ID,
        window=WINDOW,
        frame_refs=(Path("/var/empty/moment-review-never-fetched.png").as_uri(),),
    )


def test_assessment_garbage_response_is_typed_rejection() -> None:
    for garbage in (b"not-json", b'{"assessment": {}}', b"[]"):
        provider = build_assessment_call(
            PIN,
            FakeHttpTransport(response=garbage),
            AssessmentTransportConfig(endpoint=ENDPOINT),
        )
        with pytest.raises(AssessmentBadResponseError):
            provider.assess(_bundle())


def test_transport_failure_is_typed_rejection() -> None:
    provider = build_assessment_call(
        PIN, ExplodingTransport(), AssessmentTransportConfig(endpoint=ENDPOINT)
    )
    with pytest.raises(AssessmentTransportError):
        provider.assess(_bundle())


# ------------------------------------------- codex-exec assessment call (v4.4)


@dataclass(slots=True)
class _RecordedCodexCall:
    prompt: str
    model: str
    images: tuple[Path, ...]
    timeout_s: float


class FakeCodexRunner:
    """Replays canned final messages/exceptions; records every call."""

    def __init__(self, results: list[str | BaseException]) -> None:
        self._results = list(results)
        self.calls: list[_RecordedCodexCall] = []

    def __call__(
        self, prompt: str, *, model: str, images: tuple[Path, ...], timeout_s: float
    ) -> str:
        self.calls.append(_RecordedCodexCall(prompt, model, tuple(images), timeout_s))
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return str(result)


def test_codex_assessment_happy_path_resolves_frame_refs_to_attachments() -> None:
    """Given: a codex runner replaying the canned assessment (fenced); Then:
    the frames become -i attachments (never embedded payload bytes), the
    prompt carries the evidence as DATA, and the outcome validates."""

    frame = tmp_frame = Path("/var/empty/moment-review-never-fetched.png")
    runner = FakeCodexRunner(
        ["```json\n" + json.dumps(CANNED_RESPONSE) + "\n```"]
    )
    provider = build_assessment_call_codex(PIN, runner)
    bundle = AssessmentEvidenceBundle(
        episode_id=EPISODE_ID,
        window=WINDOW,
        frame_refs=(frame.as_uri(),),
        transcript_segments=(
            TranscriptSegmentText(
                segment_id="tr-1", start_frame=6, end_frame=12, text="本編セリフ"
            ),
        ),
    )

    outcome = provider.assess(bundle)

    assert outcome.assessment.subject_action_evolution == CANNED_RESPONSE["assessment"][
        "subject_action_evolution"
    ]
    call = runner.calls[0]
    assert call.model == "gpt-5.6-sol"
    assert call.images == (tmp_frame,)  # file:// ref resolved to a real path
    assert call.timeout_s == 120.0
    assert call.prompt.startswith(PROMPT_MOMENT_REVIEW)
    assert "OUTPUT CONTRACT" in call.prompt
    assert "tr-1" in call.prompt  # evidence rides as DATA
    assert "本編セリフ" in call.prompt
    assert "\\x89PNG" not in call.prompt  # no embedded image bytes, refs only


def test_codex_assessment_parse_failure_retries_once_then_typed() -> None:
    """Given: garbage, then still-garbage final messages; Then: exactly two
    runner calls and a typed AssessmentBadResponseError recording the retry."""

    runner = FakeCodexRunner(["no json", "[1, 2]"])
    provider = build_assessment_call_codex(PIN, runner)
    with pytest.raises(AssessmentBadResponseError, match="1 retry"):
        provider.assess(_bundle())
    assert len(runner.calls) == 2


def test_codex_assessment_transport_failure_is_typed_no_retry() -> None:
    runner = FakeCodexRunner([TimeoutError("codex exec hung")])
    provider = build_assessment_call_codex(PIN, runner)
    with pytest.raises(AssessmentTransportError, match="codex exec hung"):
        provider.assess(_bundle())
    assert len(runner.calls) == 1  # transport failures are never retried


def test_window_beyond_source_is_still_typed_via_record_review(
    api: MediaQueryApiV2, real_media: Path, tmp_path: Path
) -> None:
    setup, _transport = _real_setup(api, real_media, tmp_path)
    beyond = ReviewWindow(start_frame=24, end_frame=31)  # samples 24..29 exist
    with pytest.raises(WindowOutOfBoundsError):
        execute_real_review(beyond, setup, context=_context())


def test_extractor_beyond_decoded_frames_is_typed_error(real_media: Path, tmp_path: Path) -> None:
    extractor = RealFrameExtractor(
        media_path=real_media,
        media_sha256=sha256_file(real_media),
        analysis_dir=tmp_path,
        density=4,
    )
    with pytest.raises(MomentReviewRealError, match="decoded evidence lacks frame"):
        extractor.extract(ReviewWindow(start_frame=28, end_frame=40))


def test_extractor_decodes_only_the_review_window(
    real_media: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given: the real 30-frame fixture; Then: extraction decodes exactly
    the window's frames (accurate-seek window decode), never the whole
    mezzanine — the PRD §2.5 measured blocker behind round-1 BLOCKED."""

    calls: list[tuple[int, int]] = []
    real_decode = real_module.decode_luma_window

    def recording_decode(media, facts, *, tools, start_frame, end_frame):
        calls.append((start_frame, end_frame))
        return real_decode(
            media, facts, tools=tools, start_frame=start_frame, end_frame=end_frame
        )

    monkeypatch.setattr(real_module, "decode_luma_window", recording_decode)
    extractor = RealFrameExtractor(
        media_path=real_media,
        media_sha256=sha256_file(real_media),
        analysis_dir=tmp_path,
        density=4,
    )
    entries = extractor.extract(ReviewWindow(start_frame=6, end_frame=24))

    assert calls == [(6, 24)]  # window-bounded, not (0, 30)
    assert len(entries) == 4
    for entry in entries:
        assert 6 <= entry.frame < 24
        assert Path(entry.ref.removeprefix("file://")).is_file()


# ------------------------------------------------------------ static contracts


def test_module_stays_decoupled_and_network_free() -> None:
    source = inspect.getsource(real_module)
    assert "services.editorial_v2" not in source

    # urllib.parse is pure string handling (no network I/O) and explicitly
    # allowed anywhere by the services-wide AST guard (translator.py
    # precedent): the codex provider uses unquote to resolve file:// frame
    # refs. Every other urllib/network import stays forbidden.
    def _is_parse_only(import_line: str) -> bool:
        return import_line.startswith("from urllib.parse")

    for network_import in ("urllib", "http.client", "socket", "httpx", "requests"):
        for match in re.finditer(rf"^\s*(import|from) {network_import}.*$", source, re.MULTILINE):
            assert _is_parse_only(match.group(0)), f"network import: {match.group(0)}"
    assert re.findall(r"^from urllib\.parse import .*$", source, re.MULTILINE) == [
        "from urllib.parse import unquote"
    ]
    forbidden_defs = re.compile(
        r"^(\s*)?(async )?def (mutate|apply|commit|write|insert|update|delete|patch)\w*",
        re.MULTILINE,
    )
    assert forbidden_defs.search(source) is None
