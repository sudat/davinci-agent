"""T4 local provider adapters: Gemini lead + GLM visual specialist.

Two INDEPENDENT adapters over their own T3 pins (no base class, no shared
wire format — the surfaces stay exactly as live-proven in T2). Both follow
the ``CodexAssessmentProvider`` discipline: the pin env gate
(``require_pin_env``) and per-data-class cloud authorization
(``authorize_cloud_transport``) run BEFORE any transport; local evidence
is verified — including an ACTUAL pinned-ffprobe stream inspection, so a
forged ``audio_present`` flag can never ride the wire — before upload;
total serialized request bytes are bounded; transport failures are typed,
never retried, and never echo exception text.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.editorial_v2.editorial_pins import (
    EditorialPinV2,
    HttpPost,
    require_pin_env,
)
from services.media_intelligence.video_clip_evidence import (
    VerifiedLocalClip,
    VideoClipError,
    VideoClipEvidence,
    verify_local_clip,
)
from services.media_intelligence.video_review_exchange import (
    DEFAULT_MAX_MEDIA_BYTES,
    DEFAULT_MAX_REQUEST_BYTES,
    DEFAULT_MAX_RESPONSE_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    WireCall,
    WireOutcome,
    bounded_request,
    exchange,
    exchange_with_trace,
)
from services.media_intelligence.video_review_wire import (
    GEMINI_STAGE_PURPOSES,
    MOMENT_REVIEW_SPECIALIST_STAGE,
    MOMENT_REVIEW_STAGE,
    VideoProviderError,
    gemini_request_body,
    gemini_response_parser,
    glm_request_body,
    glm_response_parser,
)
from services.media_intelligence.video_stage_wire import (
    GeminiStageResult,
    stage_request_body,
    stage_response_parser,
)
from services.policy.data_policy import authorize_cloud_transport

if TYPE_CHECKING:
    from collections.abc import Mapping

    from services.config.models import DataClass, ResolvedConfig
    from services.media_intelligence.video_review_wire import (
        GeminiClipReview,
        GlmClipObservation,
    )

_GEMINI_SURFACE: Final = "google-gemini-developer-api-generate-content-rest-v1beta"
_GLM_SURFACE: Final = "openai-compatible-chat-completions-v4-coding-plan"


def _authorize(
    policy: ResolvedConfig,
    *,
    data_classes: tuple[DataClass, ...],
    stage: str,
    episode_id: str,
) -> None:
    """Authorize every data class this call ships; deny-by-default, typed."""

    for data_class in data_classes:
        decision = authorize_cloud_transport(
            policy, data_class=data_class, stage=stage, episode_id=episode_id
        )
        if not decision.allowed:
            raise VideoProviderError("cloud-policy-denied", decision.reason)


def _endpoint_of(pin: EditorialPinV2, surface: str) -> str:
    if pin.api_surface != surface:
        raise VideoProviderError(
            "production-model-unavailable",
            f"pin {pin.purpose} carries api_surface {pin.api_surface!r}; this provider "
            f"requires the independently pinned {surface!r} — never interchangeable",
        )
    if pin.endpoint is None:
        raise VideoProviderError(
            "production-model-unavailable",
            f"pin {pin.purpose} declares no endpoint; refusing rather than guessing a URL",
        )
    return pin.endpoint


def _media_base64(path: Path, max_bytes: int, what: str) -> str:
    payload = path.read_bytes()
    if len(payload) > max_bytes:
        raise VideoProviderError(
            "provider-request-oversize",
            f"the {what} clip is {len(payload)} bytes, over the {max_bytes}-byte inline "
            "bound; extract a shorter window",
        )
    return base64.b64encode(payload).decode("ascii")


def _verified(clip: VideoClipEvidence, *, audio_forbidden: bool) -> VerifiedLocalClip:
    try:
        return verify_local_clip(clip, audio_forbidden=audio_forbidden)
    except VideoClipError as error:
        raise VideoProviderError("local-evidence-rejected", str(error)) from None


@dataclass(frozen=True, slots=True)
class GeminiVideoReviewProvider:
    """Gemini lead provider over one pin; the stage purpose rides the request
    as a machine-consumed label, and the ``audio`` data class is authorized
    only when the VERIFIED clip carries audio (probed facts, not the flag)."""

    pin: EditorialPinV2
    transport: HttpPost
    policy: ResolvedConfig
    env: Mapping[str, str]
    episode_id: str
    timeout_s: float = REQUEST_TIMEOUT_SECONDS
    max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def _gates(self, clip: VideoClipEvidence) -> tuple[str, str]:
        """Endpoint/env/cloud/evidence gates + the base64 media (shared)."""

        endpoint = _endpoint_of(self.pin, _GEMINI_SURFACE)
        require_pin_env(self.pin, self.env)
        verified = _verified(clip, audio_forbidden=False)
        data_classes: tuple[DataClass, ...] = (
            ("original_video", "audio") if verified.has_audio else ("original_video",)
        )
        _authorize(
            self.policy,
            data_classes=data_classes,
            stage=MOMENT_REVIEW_STAGE,
            episode_id=self.episode_id,
        )
        return endpoint, _media_base64(verified.path, self.max_media_bytes, "Gemini")

    def _post_with_trace(
        self, body: bytes, endpoint: str, parse: Callable[[bytes], GeminiStageResult]
    ) -> WireOutcome[GeminiStageResult]:
        return exchange_with_trace(
            WireCall(
                transport=self.transport,
                endpoint=endpoint,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": self.env[self.pin.external_credentials[0]],
                },
                body=bounded_request(body, self.max_request_bytes, "Gemini"),
                timeout_s=self.timeout_s,
                max_response_bytes=self.max_response_bytes,
                what="Gemini moment-review call",
            ),
            parse,
        )

    def review_with_trace(
        self, purpose: str, clip: VideoClipEvidence, untrusted_data: str
    ) -> WireOutcome[GeminiStageResult]:
        """Stage-specific wire: the purpose selects its OWN bounded response
        schema and strict parser; the trace keeps the real attempt count."""

        if purpose not in GEMINI_STAGE_PURPOSES:
            raise VideoProviderError(
                "production-model-unavailable",
                f"unknown Gemini stage purpose {purpose!r}; allowed: "
                "local_map, global_reduce, fusion",
            )
        endpoint, media = self._gates(clip)
        body = stage_request_body(purpose, untrusted_data, media)
        return self._post_with_trace(body, endpoint, stage_response_parser(purpose, clip))

    def review(
        self, purpose: str, clip: VideoClipEvidence, untrusted_data: str
    ) -> GeminiClipReview:
        """T4-compatible path: the generic bounded schema for every purpose."""

        if purpose not in GEMINI_STAGE_PURPOSES:
            raise VideoProviderError(
                "production-model-unavailable",
                f"unknown Gemini stage purpose {purpose!r}; allowed: "
                "local_map, global_reduce, fusion",
            )
        endpoint, media = self._gates(clip)
        body = bounded_request(
            gemini_request_body(purpose, untrusted_data, media),
            self.max_request_bytes,
            "Gemini",
        )
        return exchange(
            WireCall(
                transport=self.transport,
                endpoint=endpoint,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": self.env[self.pin.external_credentials[0]],
                },
                body=body,
                timeout_s=self.timeout_s,
                max_response_bytes=self.max_response_bytes,
                what="Gemini moment-review call",
            ),
            gemini_response_parser(clip),
        )


@dataclass(frozen=True, slots=True)
class GlmVisualSpecialistProvider:
    """GLM specialist provider: video-ONLY clips (zero probed audio)."""

    pin: EditorialPinV2
    transport: HttpPost
    policy: ResolvedConfig
    env: Mapping[str, str]
    episode_id: str
    timeout_s: float = REQUEST_TIMEOUT_SECONDS
    max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def observe_with_trace(
        self, clip: VideoClipEvidence, untrusted_data: str
    ) -> WireOutcome[GlmClipObservation]:
        """Video-only observation returning the payload AND attempt count."""

        endpoint = _endpoint_of(self.pin, _GLM_SURFACE)
        require_pin_env(self.pin, self.env)
        verified = _verified(clip, audio_forbidden=True)
        _authorize(
            self.policy,
            data_classes=("original_video",),
            stage=MOMENT_REVIEW_SPECIALIST_STAGE,
            episode_id=self.episode_id,
        )
        media = _media_base64(verified.path, self.max_media_bytes, "GLM")
        body = bounded_request(
            glm_request_body(self.pin.model_id, untrusted_data, media),
            self.max_request_bytes,
            "GLM",
        )
        return exchange_with_trace(
            WireCall(
                transport=self.transport,
                endpoint=endpoint,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.env[self.pin.external_credentials[0]]}",
                },
                body=body,
                timeout_s=self.timeout_s,
                max_response_bytes=self.max_response_bytes,
                what="GLM visual-specialist call",
            ),
            glm_response_parser(clip),
        )

    def observe(self, clip: VideoClipEvidence, untrusted_data: str) -> GlmClipObservation:
        """T4-compatible payload-only observation (delegates to the trace)."""

        return self.observe_with_trace(clip, untrusted_data).payload


__all__ = [
    "GeminiVideoReviewProvider",
    "GlmVisualSpecialistProvider",
]
