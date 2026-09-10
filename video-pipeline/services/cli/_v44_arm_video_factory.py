"""Arm-B video-understanding factory — bounded connection of T3/T4/T6 blocks.

Builds a ``VideoUnderstandingFactory`` from the already implemented pins,
adapters, clip extractor, transcript/audio readers, Edit Source, and HTTP
transport. No provider hierarchy, no framework, no fallback, no live calls
at import time.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.cli.real_policy import video_understanding_policy
from services.cli.v44_arm_stages import ArmPipelineData, ArmPipelineError
from services.editorial_v2.editorial_pins import (
    EditorialPinV2,
    EditorialRuntimeError,
    EditorialRuntimeV1,
    HttpPost,
    load_editorial_pin,
    require_pin_env,
)
from services.editorial_v2.model_provider import (
    EditorialHttpResponseError,
    EditorialRedirectRefusedError,
)
from services.foundation_io import sha256_file
from services.media_intelligence.gemini_av_observation import (
    GEMINI_AV_HOST,
    GeminiAvHttpResponse,
)
from services.media_intelligence.moment_review_real import RealAudioContext, RealTranscriptLookup
from services.media_intelligence.video_clip_extraction import ClipExtractor
from services.media_intelligence.video_review_providers import (
    GeminiVideoReviewProvider,
    GlmVisualSpecialistProvider,
)
from services.media_intelligence.video_understanding_models import VideoUnderstandingDeps

if TYPE_CHECKING:
    from services.media_intelligence.video_clip_evidence import VideoClipEvidence
    from services.media_intelligence.video_review_exchange import WireOutcome
    from services.media_intelligence.video_review_wire import GlmClipObservation
    from services.media_intelligence.video_stage_wire import GeminiStageResult
    from services.media_query.query_v2 import MediaQueryApiV2

_GEMINI_ENDPOINT: Final = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.7-flash:generateContent"
)
_GLM_ENDPOINT: Final = "https://api.z.ai/api/coding/paas/v4/chat/completions"
_ALLOWED_ENDPOINTS: Final = frozenset({_GEMINI_ENDPOINT, _GLM_ENDPOINT})
_ERROR_BODY_LIMIT: Final = 300


def _require_pinned(url: str) -> None:
    if url not in _ALLOWED_ENDPOINTS:
        allowlist = ", ".join(sorted(_ALLOWED_ENDPOINTS))
        raise ValueError(
            f"endpoint-not-pinned: live video endpoint {url!r} is not pinned: "
            f"the exact HTTPS endpoint must be one of [{allowlist}]"
        )


class _PinnedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, *_args: object, **_kwargs: object
    ) -> urllib.request.Request | None:
        raise EditorialRedirectRefusedError("")


class _VideoHttpPost:
    __slots__ = ()

    def __call__(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout_s: float
    ) -> bytes:
        _require_pinned(url)
        request = urllib.request.Request(  # noqa: S310
            url, data=body, headers=dict(headers), method="POST"
        )
        try:
            with urllib.request.build_opener(_PinnedRedirectHandler()).open(
                request, timeout=timeout_s
            ) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = error.read()[:_ERROR_BODY_LIMIT].decode("utf-8", "replace")
            raise EditorialHttpResponseError(error.code, detail) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TimeoutError(
                    f"the pinned video endpoint timed out after {timeout_s:.0f}s"
                ) from error
            raise EditorialRuntimeError(
                "production-model-unavailable",
                f"the pinned video endpoint is unreachable: {error.reason}",
            ) from error


def make_video_http_post() -> HttpPost:
    return _VideoHttpPost()


def _require_gemini_av_url(url: str) -> None:
    if not url.startswith(GEMINI_AV_HOST + "/"):
        raise ValueError(
            "endpoint-not-pinned: Gemini AV transport only reaches "
            f"{GEMINI_AV_HOST} (got a different host; refusing)"
        )


class _GeminiAvHttp:
    """Header-capable transport for the Gemini AV wire (Files API needs the
    ``X-Goog-Upload-URL`` response header, which the bytes-only ``HttpPost``
    cannot surface). Host-pinned to the pinned Gemini API host; redirects
    refused; error bodies truncated — values never logged by callers."""

    __slots__ = ()

    def _roundtrip(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None,
        timeout_s: float,
    ) -> GeminiAvHttpResponse:
        _require_gemini_av_url(url)
        request = urllib.request.Request(  # noqa: S310
            url, data=body, headers=dict(headers), method=method
        )
        try:
            with urllib.request.build_opener(_PinnedRedirectHandler()).open(
                request, timeout=timeout_s
            ) as response:
                return GeminiAvHttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as error:
            detail = error.read()[:_ERROR_BODY_LIMIT].decode("utf-8", "replace")
            raise EditorialHttpResponseError(error.code, detail) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TimeoutError(
                    f"the pinned Gemini AV endpoint timed out after {timeout_s:.0f}s"
                ) from error
            raise EditorialRuntimeError(
                "production-model-unavailable",
                f"the pinned Gemini AV endpoint is unreachable: {error.reason}",
            ) from error

    def post(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout_s: float
    ) -> GeminiAvHttpResponse:
        return self._roundtrip("POST", url, headers, body, timeout_s)

    def get(
        self, url: str, headers: Mapping[str, str], timeout_s: float
    ) -> GeminiAvHttpResponse:
        return self._roundtrip("GET", url, headers, None, timeout_s)


def make_gemini_av_http() -> _GeminiAvHttp:
    return _GeminiAvHttp()


@dataclass(slots=True)
class _GeminiCaller:
    pin: EditorialPinV2
    _inner: GeminiVideoReviewProvider

    def review_with_trace(
        self, purpose: str, clip: VideoClipEvidence, untrusted_data: str
    ) -> WireOutcome[GeminiStageResult]:
        return self._inner.review_with_trace(purpose, clip, untrusted_data)


@dataclass(slots=True)
class _SpecialistCaller:
    pin: EditorialPinV2
    _inner: GlmVisualSpecialistProvider

    def observe_with_trace(
        self, clip: VideoClipEvidence, untrusted_data: str
    ) -> WireOutcome[GlmClipObservation]:
        return self._inner.observe_with_trace(clip, untrusted_data)


def build_video_understanding_factory(
    *,
    episode_id: str,
    workspace: Path,
    env: dict[str, str],
    runtime: EditorialRuntimeV1,
    runtime_path: Path,
) -> Callable[[ArmPipelineData, MediaQueryApiV2], VideoUnderstandingDeps]:
    base = runtime_path.parent.parent
    gemini_pin_path = runtime.moment_review_pin_path
    glm_pin_path = runtime.moment_review_specialist_pin_path
    if gemini_pin_path is None or glm_pin_path is None:
        raise EditorialRuntimeError(
            "production-model-unavailable",
            "editorial runtime missing moment-review pin paths; refusing",
        )
    gemini_pin = load_editorial_pin(base / gemini_pin_path)
    glm_pin = load_editorial_pin(base / glm_pin_path)
    require_pin_env(gemini_pin, env)
    require_pin_env(glm_pin, env)
    transport = make_video_http_post()
    policy = video_understanding_policy(episode_id)

    def factory(data: ArmPipelineData, speech_api: MediaQueryApiV2) -> VideoUnderstandingDeps:
        if data.mezzanine is None or data.mezzanine_sha256 is None:
            raise ArmPipelineError(
                "video-understanding-missing-mezzanine",
                "real Edit Source mezzanine is required for Arm B video understanding",
            )
        mezzanine = Path(data.mezzanine)
        if not mezzanine.is_file():
            raise ArmPipelineError(
                "video-understanding-missing-mezzanine",
                f"mezzanine not found at {mezzanine}",
            )
        actual = sha256_file(mezzanine)
        if actual != data.mezzanine_sha256:
            raise ArmPipelineError(
                "video-understanding-hash-mismatch",
                "real Edit Source mezzanine hash mismatch; refusing",
            )
        clips = ClipExtractor(media_path=mezzanine, workspace_dir=workspace)
        transcripts = RealTranscriptLookup(api=speech_api, source_id=data.source_id)
        audio = RealAudioContext(api=speech_api)
        gemini_inner = GeminiVideoReviewProvider(
            pin=gemini_pin, transport=transport, policy=policy, env=env, episode_id=episode_id
        )
        glm_inner = GlmVisualSpecialistProvider(
            pin=glm_pin, transport=transport, policy=policy, env=env, episode_id=episode_id
        )
        gemini = _GeminiCaller(pin=gemini_pin, _inner=gemini_inner)
        glm = _SpecialistCaller(pin=glm_pin, _inner=glm_inner)
        return VideoUnderstandingDeps(
            gemini=gemini, specialist=glm, clips=clips, transcripts=transcripts, audio=audio
        )

    return factory
