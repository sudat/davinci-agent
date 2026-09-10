"""Dual-route observation selection + AV consent gate (PRD v4.4 §8.5).

Two observation routes, no generic router:

- ``visual`` — the existing GLM silent-chunk path (audio never sent).
- ``audiovisual`` — ONE whole-video Gemini call over a low-res A/V proxy.

Selection uses EXISTING data only (no new classification AI): an explicit
override wins; otherwise ``auto`` picks ``audiovisual`` only when the
episode both carries a speech transcript AND the adopted consultation
policy mentions sound/voice-related terms — every other combination stays
``visual``.

The audiovisual route additionally requires the episode's explicit
original_video+original_audio cloud-send consent
(``av-analysis-consent.json`` at the episode root, mirroring the
``editorial-grant.json`` convention: explicit file > nothing — no env, no
default). Transcripts and policy text are untrusted DATA throughout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal

from services.contracts.primitives import Identifier, StrictModel

#: Wire/internal route request: ``auto`` selects from existing data.
RequestedRoute = Literal["auto", "visual", "audiovisual"]

#: Resolved route: exactly one observation path runs per request.
ObservationRoute = Literal["visual", "audiovisual"]

#: Sound/voice-related policy terms (substring match, casefolded).
#: Deliberately small and literal — not a classifier.
AUDIO_POLICY_TERMS: Final[tuple[str, ...]] = (
    "音",
    "声",
    "bgm",
    "音楽",
    "narration",
    "ナレーション",
)

AV_CONSENT_NAME: Final = "av-analysis-consent.json"


def select_observation_route(
    *,
    override: RequestedRoute | None,
    has_transcript: bool,
    policy_text: str,
) -> ObservationRoute:
    """Resolve the observation route from existing data (pure).

    An explicit ``visual``/``audiovisual`` override always wins. ``auto``
    (or absent) yields ``audiovisual`` only when the episode has a speech
    transcript AND the adopted policy mentions sound/voice terms;
    otherwise ``visual``.
    """

    if override == "visual":
        return "visual"
    if override == "audiovisual":
        return "audiovisual"
    lowered = policy_text.casefold()
    mentions_audio = any(term in lowered for term in AUDIO_POLICY_TERMS)
    if has_transcript and mentions_audio:
        return "audiovisual"
    return "visual"


class AvAnalysisConsentV1(StrictModel):
    """Per-episode original_video+original_audio cloud-send consent.

    Persisted as ``<episode-dir>/av-analysis-consent.json`` by the
    operator (see the report for the exact command). Consent is an
    explicit ``cloud_media_av_analysis: true`` — absent file, parse
    failure, episode mismatch, or any non-true value all read as
    no-consent (deny-by-default, never inferred).
    """

    schema_version: Literal["episode-av-analysis-consent-v1"] = (
        "episode-av-analysis-consent-v1"
    )
    episode_id: Identifier
    cloud_media_av_analysis: bool
    granted_at: str = ""
    note: str = ""


def read_av_consent(episode_dir: Path, episode_id: str) -> bool:
    """True only when the episode's consent file explicitly allows AV send."""

    path = episode_dir / AV_CONSENT_NAME
    if not path.is_file():
        return False
    try:
        consent = AvAnalysisConsentV1.model_validate_json(path.read_bytes())
    except ValueError:
        return False
    return bool(
        consent.episode_id == episode_id and consent.cloud_media_av_analysis is True
    )


__all__ = [
    "AUDIO_POLICY_TERMS",
    "AV_CONSENT_NAME",
    "AvAnalysisConsentV1",
    "ObservationRoute",
    "RequestedRoute",
    "read_av_consent",
    "select_observation_route",
]
