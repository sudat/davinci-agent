# allow: SIZE_OK — the v44 T4 plan names the provider adapter contract tests;
# neighboring test modules in this repo run longer (test_moment_review_real.py
# 576, test_model_provider.py 721) and splitting the contract would scatter
# one adapter surface across files.
"""T4 local provider adapters (Gemini lead / GLM specialist) — Tier A.

Deterministic, network-free: the transport is an injected fake at the wire
seam (no live provider call — T2 capability probes are the live proof) and
the evidence clips are REAL pinned-ffmpeg mp4s, because the providers now
verify ACTUAL streams (a forged ``audio_present`` flag must never ride the
wire). Proves the exact T2-proven surfaces from the SHIPPED T3 pins, the
trusted-instruction/untrusted-data part separation, the total serialized
request bound, and every named typed failure: disabled env gate, denied
cloud policy, wrong-surface pin, actual-audio evidence at GLM, forged
audio declarations, missing local file, redirect, timeout, HTTP status,
unknown transport exceptions (never echoed), malformed responses (one
retry), empty/oversize responses (never retried), and requested/analyzed
range mismatch. No credential value, base64 media, or remote URL ever
appears in a raised error.
"""

from __future__ import annotations

import base64
import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from pydantic import ValidationError

from services.analyze.audio_probe import PinnedAudioTools, resolve_audio_tools
from services.cli.real_policy import (
    MOMENT_REVIEW_SPECIALIST_STAGE as POLICY_SPECIALIST_STAGE,
)
from services.cli.real_policy import (
    MOMENT_REVIEW_STAGE as POLICY_LEAD_STAGE,
)
from services.cli.real_policy import local_only_policy, video_understanding_policy
from services.editorial_v2.editorial_pins import (
    MOMENT_REVIEW_PIN_PATH,
    MOMENT_REVIEW_SPECIALIST_PIN_PATH,
    EditorialHttpResponseError,
    EditorialRedirectRefusedError,
    EditorialRuntimeError,
    load_editorial_pin,
)
from services.foundation_io import sha256_file
from services.media_intelligence.moment_review import ReviewWindow
from services.media_intelligence.video_clip_evidence import VideoClipEvidence
from services.media_intelligence.video_review_exchange import (
    DEFAULT_MAX_MEDIA_BYTES,
    DEFAULT_MAX_REQUEST_BYTES,
    DEFAULT_MAX_RESPONSE_BYTES,
)
from services.media_intelligence.video_review_providers import (
    GeminiVideoReviewProvider,
    GlmVisualSpecialistProvider,
)
from services.media_intelligence.video_review_wire import (
    GEMINI_REVIEW_INSTRUCTIONS,
    GEMINI_STAGE_PURPOSES,
    GLM_OUTPUT_CONTRACT,
    GLM_SPECIALIST_INSTRUCTIONS,
    MOMENT_REVIEW_SPECIALIST_STAGE,
    MOMENT_REVIEW_STAGE,
    UNTRUSTED_DATA_MARKER,
    GeminiClipReview,
    GlmClipObservation,
    VideoProviderError,
    gemini_request_body,
)
from services.media_intelligence.video_stage_wire import (
    GEMINI_FUSION_INSTRUCTIONS,
    GEMINI_GLOBAL_REDUCE_INSTRUCTIONS,
    GeminiEpisodeReduce,
    GeminiFusionReview,
)

if TYPE_CHECKING:
    from services.config.models import ResolvedConfig

EPISODE_ID = "v44-real-01"
WINDOW: Final = ReviewWindow(start_frame=7, end_frame=37)
GEMINI_KEY = "gemini-key-value-never-leaked"
ZAI_KEY = "zai-key-value-never-leaked"
GEMINI_ENV: Final[dict[str, str]] = {"GEMINI_API_KEY": GEMINI_KEY, "GEMINI_NETWORK_ENABLED": "1"}
ZAI_ENV: Final[dict[str, str]] = {"ZAI_API_KEY": ZAI_KEY, "ZAI_NETWORK_ENABLED": "1"}


# ------------------------------------------------------------ real media + fakes


@pytest.fixture(scope="session")
def pinned_tools() -> PinnedAudioTools:
    try:
        return resolve_audio_tools()
    except (OSError, ValueError) as error:
        pytest.skip(f"pinned phase-1 ffmpeg not bootstrapped: {error}")


def _encode_media(tools: PinnedAudioTools, target: Path, *, with_audio: bool) -> Path:
    argv = [
        str(tools.ffmpeg),
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x120:rate=15:duration=1",
    ]
    if with_audio:
        argv += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    argv += ["-c:v", "h264_videotoolbox", "-pix_fmt", "yuv420p"]
    if with_audio:
        argv += ["-c:a", "aac", "-shortest"]
    argv += [str(target)]
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-800:]
    return target


@pytest.fixture(scope="session")
def real_av_media(pinned_tools: PinnedAudioTools, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Real H.264+AAC mp4 with an ACTUAL audio stream."""

    return _encode_media(
        pinned_tools, tmp_path_factory.mktemp("t4-providers") / "av.mp4", with_audio=True
    )


@pytest.fixture(scope="session")
def real_silent_media(
    pinned_tools: PinnedAudioTools, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """Real H.264 mp4 with ZERO audio streams (the GLM-legal payload)."""

    return _encode_media(
        pinned_tools, tmp_path_factory.mktemp("t4-providers") / "silent.mp4", with_audio=False
    )


def _clip(media: Path, *, audio_present: bool | None = None) -> VideoClipEvidence:
    """Evidence over REAL bytes; ``audio_present`` defaults to the truth."""

    return VideoClipEvidence(
        ref=media.as_uri(),
        sha256=sha256_file(media),
        requested_range=WINDOW,
        analyzed_range=WINDOW,
        audio_present=audio_present if audio_present is not None else _probe_has_audio(media),
        duration_seconds=1.001,
    )


def _probe_has_audio(media: Path) -> bool:
    tools = resolve_audio_tools()
    result = subprocess.run(
        (str(tools.ffprobe), "-v", "error", "-print_format", "json", "-show_streams", str(media)),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-400:]
    streams: list[dict[str, object]] = json.loads(result.stdout)["streams"]
    return any(stream.get("codec_type") == "audio" for stream in streams)


@dataclass(slots=True)
class FakeTransport:
    results: list[bytes | BaseException]
    calls: list[tuple[str, dict[str, str], bytes, float]] = field(default_factory=list)

    def __call__(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout_s: float
    ) -> bytes:
        self.calls.append((url, dict(headers), body, timeout_s))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _gemini_wire(review: dict[str, object]) -> bytes:
    return _gemini_wire_text(json.dumps(review))


def _gemini_wire_text(text: str) -> bytes:
    return json.dumps(
        {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": text}]},
                    "finishReason": "STOP",
                }
            ]
        }
    ).encode()


def _glm_wire_content(content: str) -> bytes:
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "model": "glm-5v-turbo",
        }
    ).encode()


def _glm_wire(observation: dict[str, object]) -> bytes:
    return _glm_wire_content(json.dumps(observation))


def _gemini_review() -> dict[str, object]:
    return {
        "analyzed_start_frame": 7,
        "analyzed_end_frame": 37,
        "summary": "pattern sweeps across the interval",
        "observations": ["steady motion", "no scene change"],
        "audio_note": "continuous tone",
    }


def _glm_observation() -> dict[str, object]:
    return {
        "analyzed_start_frame": 7,
        "analyzed_end_frame": 37,
        "visual_findings": ["on-screen counter advances"],
        "uncertainty": "small text legibility",
    }


@dataclass(frozen=True, slots=True)
class _ProviderLimits:
    """Per-test overrides of the provider bounds (defaults = production)."""

    max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES


_DEFAULT_LIMITS: Final = _ProviderLimits()


def _gemini_provider(
    transport: FakeTransport,
    env: Mapping[str, str] = GEMINI_ENV,
    *,
    policy: ResolvedConfig | None = None,
    limits: _ProviderLimits = _DEFAULT_LIMITS,
) -> GeminiVideoReviewProvider:
    return GeminiVideoReviewProvider(
        pin=load_editorial_pin(MOMENT_REVIEW_PIN_PATH),
        transport=transport,
        policy=policy if policy is not None else video_understanding_policy(EPISODE_ID),
        env=env,
        episode_id=EPISODE_ID,
        max_media_bytes=limits.max_media_bytes,
        max_request_bytes=limits.max_request_bytes,
        max_response_bytes=limits.max_response_bytes,
    )


def _glm_provider(
    transport: FakeTransport,
    env: Mapping[str, str] = ZAI_ENV,
    *,
    policy: ResolvedConfig | None = None,
    limits: _ProviderLimits = _DEFAULT_LIMITS,
) -> GlmVisualSpecialistProvider:
    return GlmVisualSpecialistProvider(
        pin=load_editorial_pin(MOMENT_REVIEW_SPECIALIST_PIN_PATH),
        transport=transport,
        policy=policy if policy is not None else video_understanding_policy(EPISODE_ID),
        env=env,
        episode_id=EPISODE_ID,
        max_media_bytes=limits.max_media_bytes,
        max_request_bytes=limits.max_request_bytes,
        max_response_bytes=limits.max_response_bytes,
    )


# ------------------------------------------------------------ stage vocabulary


def test_stage_names_match_the_t3_policy_seam() -> None:
    assert MOMENT_REVIEW_STAGE == POLICY_LEAD_STAGE == "moment_review"
    assert MOMENT_REVIEW_SPECIALIST_STAGE == POLICY_SPECIALIST_STAGE == "moment_review_specialist"
    assert frozenset({"local_map", "global_reduce", "fusion"}) == GEMINI_STAGE_PURPOSES


def test_wrong_surface_pin_is_refused_before_any_call(real_silent_media: Path) -> None:
    gemini_pin_on_glm = GlmVisualSpecialistProvider(
        pin=load_editorial_pin(MOMENT_REVIEW_PIN_PATH),
        transport=FakeTransport([]),
        policy=video_understanding_policy(EPISODE_ID),
        env=ZAI_ENV,
        episode_id=EPISODE_ID,
    )
    with pytest.raises(VideoProviderError, match="surface") as error:
        gemini_pin_on_glm.observe(_clip(real_silent_media), "what do you see")
    assert error.value.code == "production-model-unavailable"

    glm_pin_on_gemini = GeminiVideoReviewProvider(
        pin=load_editorial_pin(MOMENT_REVIEW_SPECIALIST_PIN_PATH),
        transport=FakeTransport([]),
        policy=video_understanding_policy(EPISODE_ID),
        env=GEMINI_ENV,
        episode_id=EPISODE_ID,
    )
    with pytest.raises(VideoProviderError, match="surface"):
        glm_pin_on_gemini.review("local_map", _clip(real_silent_media), "review this")


# ------------------------------------------------------------ wire shapes


def test_gemini_request_separates_trusted_instructions_from_untrusted_data(
    real_av_media: Path,
) -> None:
    """Given: the shipped Gemini pin + a real audio-bearing clip + untrusted
    text; Then: trusted stage-labelled instructions, the untrusted document
    behind the fixed marker in its OWN part, inline_data clip, bounded
    schema — all on the exact pinned endpoint."""

    transport = FakeTransport([_gemini_wire(_gemini_review())])
    provider = _gemini_provider(transport)
    outcome = provider.review("local_map", _clip(real_av_media), "brief text DATA")

    assert outcome.analyzed_start_frame == 7
    assert outcome.analyzed_end_frame == 37
    assert outcome.audio_note == "continuous tone"

    url, headers, body, timeout_s = transport.calls[0]
    assert url == provider.pin.endpoint
    assert "gemini-3.7-flash:generateContent" in url
    assert headers["x-goog-api-key"] == GEMINI_KEY
    assert timeout_s == 120.0
    payload = json.loads(body)
    parts = payload["contents"][0]["parts"]
    assert parts[0]["text"] == f"[stage:local_map]\n{GEMINI_REVIEW_INSTRUCTIONS}"
    assert parts[1]["text"].startswith(f"{UNTRUSTED_DATA_MARKER}\n")
    assert "brief text DATA" in parts[1]["text"]
    assert parts[2]["inline_data"]["mime_type"] == "video/mp4"
    assert base64.b64decode(parts[2]["inline_data"]["data"]) == real_av_media.read_bytes()
    config = payload["generationConfig"]
    assert config["response_mime_type"] == "application/json"
    assert config["response_schema"]["properties"]["analyzed_start_frame"]


def test_gemini_stage_label_routes_every_approved_purpose(
    real_silent_media: Path,
) -> None:
    for purpose in ("local_map", "global_reduce", "fusion"):
        transport = FakeTransport([_gemini_wire(_gemini_review())])
        _gemini_provider(transport).review(purpose, _clip(real_silent_media), "data")
        text = json.loads(transport.calls[0][2])["contents"][0]["parts"][0]["text"]
        assert text.startswith(f"[stage:{purpose}]")
    transport = FakeTransport([])
    with pytest.raises(VideoProviderError, match="purpose"):
        _gemini_provider(transport).review("specialist", _clip(real_silent_media), "data")


def test_gemini_wire_schema_carries_only_provider_supported_keywords() -> None:
    """MEASURED 2026-08-30 (T11 real Arm B run): generateContent answers
    HTTP 400 INVALID_ARGUMENT "Unknown name additionalProperties at
    generation_config.response_schema" for pydantic's raw model_json_schema
    (StrictModel emits ``additionalProperties: false``). The wire schema for
    EVERY stage purpose must carry only provider-supported keywords; local
    parse-time strictness is unchanged."""

    from services.media_intelligence.video_stage_wire import (  # noqa: PLC0415
        stage_request_body,
    )

    bodies = [stage_request_body(purpose, "data", "bWVkaWE=") for purpose in GEMINI_STAGE_PURPOSES]
    bodies.append(gemini_request_body("local_map", "data", "bWVkaWE="))
    for body in bodies:
        assert b"additionalProperties" not in body, "provider parser rejects the keyword"
        assert b'"$defs"' not in body, "provider parser rejects $defs (nested models)"
        assert b'"$ref"' not in body, "provider parser rejects $ref (nested models)"
        schema = json.loads(body)["generationConfig"]["response_schema"]
        assert schema["type"] == "object"
        assert schema["properties"], "bounded schema must stay embedded"


def test_glm_request_separates_trusted_instructions_from_untrusted_data(
    real_silent_media: Path,
) -> None:
    transport = FakeTransport([_glm_wire(_glm_observation())])
    provider = _glm_provider(transport)
    outcome = provider.observe(_clip(real_silent_media), "region description DATA")

    assert outcome.visual_findings == ("on-screen counter advances",)
    assert outcome.uncertainty == "small text legibility"

    url, headers, body, timeout_s = transport.calls[0]
    assert url == provider.pin.endpoint
    assert url == "https://api.z.ai/api/coding/paas/v4/chat/completions"
    assert headers["Authorization"] == f"Bearer {ZAI_KEY}"
    assert timeout_s == 120.0
    payload = json.loads(body)
    assert payload["model"] == "glm-5v-turbo"
    assert payload["thinking"] == {"type": "disabled"}
    content = payload["messages"][0]["content"]
    assert content[0]["text"] == f"{GLM_SPECIALIST_INSTRUCTIONS}\n\n{GLM_OUTPUT_CONTRACT}"
    assert content[1]["text"].startswith(f"{UNTRUSTED_DATA_MARKER}\n")
    assert "region description DATA" in content[1]["text"]
    assert content[2]["type"] == "video_url"
    data_uri = content[2]["video_url"]["url"]
    assert data_uri.startswith("data:video/mp4;base64,")
    assert base64.b64decode(data_uri.removeprefix("data:video/mp4;base64,")) == (
        real_silent_media.read_bytes()
    )


# ------------------------------------------------------------ gates (before any transport)


def test_disabled_env_gate_is_typed_unavailable_before_any_call(
    real_silent_media: Path,
) -> None:
    for env in ({}, {"GEMINI_API_KEY": GEMINI_KEY}, GEMINI_ENV | {"GEMINI_NETWORK_ENABLED": "0"}):
        transport = FakeTransport([])
        provider = _gemini_provider(transport, env=env)
        with pytest.raises(EditorialRuntimeError) as error:
            provider.review("fusion", _clip(real_silent_media), "data")
        assert error.value.code == "production-model-unavailable"
        assert GEMINI_KEY not in error.value.detail
        assert transport.calls == []

    transport = FakeTransport([])
    with pytest.raises(EditorialRuntimeError):
        _glm_provider(transport, env={"ZAI_API_KEY": ZAI_KEY}).observe(
            _clip(real_silent_media), "data"
        )
    assert transport.calls == []


def test_denied_cloud_policy_is_typed_before_any_call(real_silent_media: Path) -> None:
    transport = FakeTransport([])
    provider = _gemini_provider(transport, policy=local_only_policy(EPISODE_ID))
    with pytest.raises(VideoProviderError, match="local_only") as error:
        provider.review("local_map", _clip(real_silent_media), "data")
    assert error.value.code == "cloud-policy-denied"
    assert transport.calls == []

    transport = FakeTransport([])
    with pytest.raises(VideoProviderError, match="local_only"):
        _glm_provider(transport, policy=local_only_policy(EPISODE_ID)).observe(
            _clip(real_silent_media), "data"
        )
    assert transport.calls == []


def test_missing_local_media_is_typed_before_any_call(
    real_silent_media: Path, tmp_path: Path
) -> None:
    gone = _clip(real_silent_media).model_copy(
        update={"ref": (tmp_path / "never-extracted.mp4").as_uri(), "sha256": "d" * 64}
    )
    transport = FakeTransport([])
    with pytest.raises(VideoProviderError, match="missing"):
        _gemini_provider(transport).review("local_map", gone, "data")
    assert transport.calls == []


# --------------------------------------- actual-stream verification (root repro 1)


def test_glm_rejects_actual_audio_stream_despite_false_declaration(
    real_av_media: Path,
) -> None:
    """Root repro 1: a real mp4 whose bytes/hash carry an ACTUAL audio stream,
    declared audio_present=False (forged/stale flag); Then: ZERO transport
    calls and a typed local-evidence rejection — probed facts beat flags."""

    forged = _clip(real_av_media, audio_present=False)
    transport = FakeTransport([_glm_wire(_glm_observation())])
    with pytest.raises(VideoProviderError) as error:
        _glm_provider(transport).observe(forged, "inspect this region")
    assert error.value.code == "local-evidence-rejected"
    assert "audio" in error.value.detail
    assert len(transport.calls) == 0  # never encoded, never transmitted


def test_glm_rejects_honest_audio_evidence_too(real_av_media: Path) -> None:
    """Even a TRUTHFUL audio declaration is refused at the specialist gate:
    the wire is video-only regardless of what the evidence claims."""

    transport = FakeTransport([])
    with pytest.raises(VideoProviderError, match="audio"):
        _glm_provider(transport).observe(_clip(real_av_media), "data")
    assert transport.calls == []


def test_declared_audio_presence_must_match_actual_streams(
    real_av_media: Path, real_silent_media: Path
) -> None:
    """Both lie directions are typed rejections: audio declared absent but
    present, and audio declared present but absent."""

    transport = FakeTransport([])
    with pytest.raises(VideoProviderError, match="audio_present"):
        _gemini_provider(transport).review(
            "local_map", _clip(real_av_media, audio_present=False), "data"
        )
    with pytest.raises(VideoProviderError, match="audio_present"):
        _gemini_provider(transport).review(
            "local_map", _clip(real_silent_media, audio_present=True), "data"
        )
    assert transport.calls == []


def test_gemini_authorizes_audio_from_verified_facts_not_the_flag(
    real_silent_media: Path,
) -> None:
    """The audio data class rides only when the VERIFIED clip carries audio:
    a silent clip never asks for the audio grant even under a policy that
    would allow it."""

    transport = FakeTransport([_gemini_wire(_gemini_review())])
    outcome = _gemini_provider(transport).review("local_map", _clip(real_silent_media), "data")
    assert outcome.analyzed_end_frame == 37
    assert len(transport.calls) == 1


# ------------------------------------------ transport failures (never retried)


def test_redirect_timeout_http_and_unknown_failures_are_typed_without_retry(
    real_silent_media: Path,
) -> None:
    cases: list[BaseException] = [
        EditorialRedirectRefusedError("https://redirect.invalid/steal"),
        TimeoutError("transport hung"),
        EditorialHttpResponseError(429, "quota"),
        ConnectionError("socket refused"),
        RuntimeError("failure included SECRET_VALUE_MUST_NOT_LEAK"),
    ]
    for failure in cases:
        transport = FakeTransport([failure])
        with pytest.raises(VideoProviderError) as error:
            _gemini_provider(transport).review("local_map", _clip(real_silent_media), "data")
        assert error.value.code.startswith("provider-")
        assert "redirect.invalid" not in error.value.detail  # remote URLs stay out
        assert "SECRET_VALUE_MUST_NOT_LEAK" not in error.value.detail  # no echo
        assert "socket refused" not in error.value.detail
        assert len(transport.calls) == 1  # transport failures are never retried


# ------------------------------------------------------------ response discipline


def test_malformed_response_retries_once_then_recovers_or_fails_typed(
    real_silent_media: Path,
) -> None:
    # recovery: garbage first, valid second → exactly two transport calls
    transport = FakeTransport([b"not-json", _gemini_wire(_gemini_review())])
    outcome = _gemini_provider(transport).review("local_map", _clip(real_silent_media), "data")
    assert outcome.analyzed_end_frame == 37
    assert len(transport.calls) == 2

    # exhaustion: garbage twice → typed bad-response recording the one retry
    transport = FakeTransport([b"not-json", b"also-not-json"])
    with pytest.raises(VideoProviderError, match="1 retry") as error:
        _gemini_provider(transport).review("local_map", _clip(real_silent_media), "data")
    assert error.value.code == "provider-bad-response"
    assert len(transport.calls) == 2

    # GLM recovery, matching live T2 behavior: the FIRST message.content is a
    # markdown-fenced object (strict parse fails); the retry returns clean JSON.
    fenced_content = f"```json\n{json.dumps(_glm_observation())}\n```"
    transport = FakeTransport([_glm_wire_content(fenced_content), _glm_wire(_glm_observation())])
    outcome = _glm_provider(transport).observe(_clip(real_silent_media), "data")
    assert outcome.visual_findings == ("on-screen counter advances",)
    assert len(transport.calls) == 2


def test_empty_and_oversize_responses_fail_immediately_without_retry(
    real_silent_media: Path,
) -> None:
    """Empty/oversize are transport-shape failures, not malformed model
    output: they raise on the FIRST response and never consume the retry."""

    transport = FakeTransport([b""])
    with pytest.raises(VideoProviderError) as error:
        _gemini_provider(transport).review("local_map", _clip(real_silent_media), "data")
    assert error.value.code == "provider-response-empty"
    assert "never retried" in error.value.detail
    assert len(transport.calls) == 1

    huge = b"x" * 33
    transport = FakeTransport([huge])
    with pytest.raises(VideoProviderError) as error:
        _gemini_provider(transport, limits=_ProviderLimits(max_response_bytes=32)).review(
            "local_map", _clip(real_silent_media), "data"
        )
    assert error.value.code == "provider-response-oversize"
    assert len(transport.calls) == 1


def test_request_oversize_is_typed_before_transport(
    real_silent_media: Path, real_av_media: Path
) -> None:
    # media bound (cheap early check on the raw clip bytes)
    transport = FakeTransport([])
    with pytest.raises(VideoProviderError) as error:
        _gemini_provider(transport, limits=_ProviderLimits(max_media_bytes=4)).review(
            "local_map", _clip(real_silent_media), "data"
        )
    assert error.value.code == "provider-request-oversize"
    assert transport.calls == []

    # total-body bound (authoritative): a 1M-char untrusted string with a
    # small permitted clip must be refused AFTER serialization, BEFORE transport
    transport = FakeTransport([])
    provider = _gemini_provider(transport, limits=_ProviderLimits(max_request_bytes=65_536))
    with pytest.raises(VideoProviderError) as error:
        provider.review("local_map", _clip(real_av_media), "x" * 1_000_000)
    assert error.value.code == "provider-request-oversize"
    assert "total bound" in error.value.detail
    assert transport.calls == []


def test_provider_range_mismatch_is_typed(real_silent_media: Path) -> None:
    """Provider-claimed analyzed ranges are observations: anything but the
    exact requested half-open interval is a typed mismatch, never trusted."""

    mismatched = dict(_gemini_review(), analyzed_start_frame=6, analyzed_end_frame=37)
    transport = FakeTransport([_gemini_wire(mismatched), _gemini_wire(mismatched)])
    with pytest.raises(VideoProviderError) as error:
        _gemini_provider(transport).review("local_map", _clip(real_silent_media), "data")
    assert error.value.code == "provider-range-mismatch"

    drift = dict(_glm_observation(), analyzed_end_frame=38)
    transport = FakeTransport([_glm_wire(drift), _glm_wire(drift)])
    with pytest.raises(VideoProviderError, match="38"):
        _glm_provider(transport).observe(_clip(real_silent_media), "data")


def test_observation_models_stay_bounded() -> None:
    with pytest.raises(ValidationError):
        GeminiClipReview.model_validate(
            {"analyzed_start_frame": 7, "analyzed_end_frame": 37, "summary": ""}
        )
    with pytest.raises(ValidationError):
        GlmClipObservation.model_validate(
            {"analyzed_start_frame": 7, "analyzed_end_frame": 37, "visual_findings": []}
        )


# ------------------------------------------------------------ T6 trace seam


def test_trace_records_two_attempts_after_malformed_retry(real_silent_media: Path) -> None:
    """A malformed-first-valid-second parser retry must survive into the
    caller-visible trace: attempts == 2, payload parsed and range-checked."""

    transport = FakeTransport([b"not-json", _gemini_wire(_gemini_review())])
    trace = _gemini_provider(transport).review_with_trace(
        "local_map", _clip(real_silent_media), "data"
    )
    assert isinstance(trace.payload, GeminiClipReview)
    assert trace.payload.analyzed_end_frame == 37
    assert trace.attempts == 2
    assert len(transport.calls) == 2

    fenced = f"```json\n{json.dumps(_glm_observation())}\n```"
    transport = FakeTransport([_glm_wire_content(fenced), _glm_wire(_glm_observation())])
    glm_trace = _glm_provider(transport).observe_with_trace(
        _clip(real_silent_media), "data"
    )
    assert glm_trace.attempts == 2
    assert glm_trace.payload.visual_findings == ("on-screen counter advances",)


def test_transport_shaped_failures_carry_single_attempt(real_silent_media: Path) -> None:
    """Empty/oversize responses and transport failures make exactly one
    attempt: the raised error says so, and no retry is consumed."""

    transport = FakeTransport([b""])
    with pytest.raises(VideoProviderError) as error:
        _gemini_provider(transport).review_with_trace(
            "local_map", _clip(real_silent_media), "data"
        )
    assert error.value.attempts == 1
    assert len(transport.calls) == 1

    transport = FakeTransport([TimeoutError("transport hung")])
    with pytest.raises(VideoProviderError) as error:
        _gemini_provider(transport).review_with_trace(
            "local_map", _clip(real_silent_media), "data"
        )
    assert error.value.attempts == 1

    transport = FakeTransport([b"not-json", b"also-not-json"])
    with pytest.raises(VideoProviderError) as error:
        _gemini_provider(transport).review_with_trace(
            "local_map", _clip(real_silent_media), "data"
        )
    assert error.value.attempts == 2  # parser exhaustion after the one retry


def test_compatibility_methods_return_payload_only(real_silent_media: Path) -> None:
    transport = FakeTransport([b"garbage", _gemini_wire(_gemini_review())])
    payload = _gemini_provider(transport).review(
        "local_map", _clip(real_silent_media), "data"
    )
    assert isinstance(payload, GeminiClipReview)
    transport = FakeTransport([_glm_wire(_glm_observation())])
    observation = _glm_provider(transport).observe(_clip(real_silent_media), "data")
    assert isinstance(observation, GlmClipObservation)


def test_stage_wire_selects_schema_and_parser_by_purpose(
    real_silent_media: Path,
) -> None:
    """The trace seam routes the MACHINE-CONSUMED purpose to its own bounded
    response schema (embedded in the request) and its own strict parser —
    provider prose is never parsed for structure."""

    reduce_payload = {
        "analyzed_start_frame": 0,
        "analyzed_end_frame": 3600,
        "summary": "episode reduce summary",
        "observations": ["region of uncertainty"],
        "audio_note": None,
        "specialist_requests": [
            {"start_frame": 7, "end_frame": 20, "rationale": "dense on-screen text",
             "uncertainty": None}
        ],
    }
    transport = FakeTransport([_gemini_wire(reduce_payload)])
    trace = _gemini_provider(transport).review_with_trace(
        "global_reduce", _clip(real_silent_media), "data"
    )
    assert isinstance(trace.payload, GeminiEpisodeReduce)
    assert trace.payload.specialist_requests[0].start_frame == 7
    schema = json.loads(transport.calls[0][2])["generationConfig"]["response_schema"]
    assert "specialist_requests" in schema["properties"]

    fusion_payload = {
        "analyzed_start_frame": 7,
        "analyzed_end_frame": 37,
        "subject_action_evolution": "subject",
        "reaction_notes": "reaction",
        "timing_notes": "timing",
        "best_sub_span": {"start_frame": 10, "end_frame": 30},
        "keep_rationale_candidates": ["keep"],
        "remove_rationale_candidates": [],
        "cut_in_handle": "in",
        "cut_out_handle": "out",
        "confidence": {"overall": 0.9, "subject_action_evolution": 0.8,
                       "reaction_notes": 0.7, "timing_notes": 0.6, "best_sub_span": 0.5},
        "unresolved_acknowledgements": [],
    }
    transport = FakeTransport([_gemini_wire(fusion_payload)])
    trace = _gemini_provider(transport).review_with_trace(
        "fusion", _clip(real_silent_media), "data"
    )
    assert isinstance(trace.payload, GeminiFusionReview)
    assert trace.payload.confidence.overall == 0.9
    schema = json.loads(transport.calls[0][2])["generationConfig"]["response_schema"]
    assert "best_sub_span" in schema["properties"]
    assert "unresolved_acknowledgements" in schema["properties"]

    drifted: dict[str, object] = dict(fusion_payload, analyzed_end_frame=38)
    transport = FakeTransport([_gemini_wire(drifted), _gemini_wire(drifted)])
    with pytest.raises(VideoProviderError) as error:
        _gemini_provider(transport).review_with_trace(
            "fusion", _clip(real_silent_media), "data"
        )
    assert error.value.code == "provider-range-mismatch"


# ------------------------------------------------------------ second-repair contracts


def test_global_reduce_wire_carries_stage_specific_instructions(
    real_silent_media: Path,
) -> None:
    """The reduce stage's TRUSTED instructions and response schema agree: the
    instructions ask for the fields the schema actually contains (full-episode
    analyzed bounds plus typed specialist requests), and are NOT the
    clip-interval instruction reused verbatim."""

    transport = FakeTransport([_gemini_wire(reduce_payload_analyzed())])
    trace = _gemini_provider(transport).review_with_trace(
        "global_reduce", _clip(real_silent_media), "data"
    )
    assert isinstance(trace.payload, GeminiEpisodeReduce)
    assert trace.payload.analyzed_start_frame == 0
    assert trace.payload.analyzed_end_frame == 3600

    payload = json.loads(transport.calls[0][2])
    parts = payload["contents"][0]["parts"]
    assert parts[0]["text"] == f"[stage:global_reduce]\n{GEMINI_GLOBAL_REDUCE_INSTRUCTIONS}"
    assert parts[0]["text"] != f"[stage:global_reduce]\n{GEMINI_REVIEW_INSTRUCTIONS}"
    schema = payload["generationConfig"]["response_schema"]
    assert "analyzed_start_frame" in schema["properties"]
    assert "analyzed_end_frame" in schema["properties"]
    assert "specialist_requests" in schema["properties"]

    fusion_text = None
    transport = FakeTransport([_gemini_wire(_fusion_wire_payload())])
    _gemini_provider(transport).review_with_trace(
        "fusion", _clip(real_silent_media), "data"
    )
    fusion_text = json.loads(transport.calls[0][2])["contents"][0]["parts"][0]["text"]
    assert fusion_text == f"[stage:fusion]\n{GEMINI_FUSION_INSTRUCTIONS}"
    assert fusion_text != f"[stage:fusion]\n{GEMINI_REVIEW_INSTRUCTIONS}"


def reduce_payload_analyzed() -> dict[str, object]:
    return {
        "analyzed_start_frame": 0,
        "analyzed_end_frame": 3600,
        "summary": "episode reduce summary",
        "observations": ["region of uncertainty"],
        "audio_note": None,
        "specialist_requests": [
            {"start_frame": 7, "end_frame": 20, "rationale": "dense on-screen text",
             "uncertainty": None}
        ],
    }


def _fusion_wire_payload() -> dict[str, object]:
    return {
        "analyzed_start_frame": 7,
        "analyzed_end_frame": 37,
        "subject_action_evolution": "subject",
        "reaction_notes": "reaction",
        "timing_notes": "timing",
        "best_sub_span": {"start_frame": 10, "end_frame": 30},
        "keep_rationale_candidates": ["keep"],
        "remove_rationale_candidates": [],
        "cut_in_handle": "in",
        "cut_out_handle": "out",
        "confidence": {"overall": 0.9, "subject_action_evolution": 0.8,
                       "reaction_notes": 0.7, "timing_notes": 0.6, "best_sub_span": 0.5},
        "unresolved_acknowledgements": [],
    }


def test_parser_error_details_never_echo_provider_content(
    real_silent_media: Path,
) -> None:
    """Schema-invalid provider output carrying secrets, remote URLs, and
    transcript-like personal text must never appear in the typed error
    detail — stable sanitized wording only, still after two attempts."""

    poisoned = json.dumps(
        {"analyzed_start_frame": "SECRET_VALUE_MUST_NOT_LEAK",
         "analyzed_end_frame": 37, "summary": "see https://evil.invalid/exfil",
         "observations": ["my home address is 1-2-3 Shimokitazawa"],
         "audio_note": None}
    )
    transport = FakeTransport([_gemini_wire_text(poisoned), _gemini_wire_text(poisoned)])
    with pytest.raises(VideoProviderError) as error:
        _gemini_provider(transport).review_with_trace(
            "local_map", _clip(real_silent_media), "data"
        )
    assert error.value.code == "provider-bad-response"
    assert error.value.attempts == 2
    assert "SECRET_VALUE_MUST_NOT_LEAK" not in error.value.detail
    assert "evil.invalid" not in error.value.detail
    assert "Shimokitazawa" not in error.value.detail

    transport = FakeTransport([_gemini_wire_text(poisoned), _gemini_wire_text(poisoned)])
    with pytest.raises(VideoProviderError) as error:
        _gemini_provider(transport).review(
            "local_map", _clip(real_silent_media), "data"
        )
    assert "SECRET_VALUE_MUST_NOT_LEAK" not in error.value.detail

    transport = FakeTransport([_glm_wire_content(poisoned), _glm_wire_content(poisoned)])
    with pytest.raises(VideoProviderError) as error:
        _glm_provider(transport).observe(_clip(real_silent_media), "data")
    assert error.value.attempts == 2
    assert "SECRET_VALUE_MUST_NOT_LEAK" not in error.value.detail
