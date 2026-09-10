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

import json
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

#: Boilerplate that marks an audio_policy sentence as NOT specifying intent.
_NO_AUDIO_INTENT_MARKERS: Final[tuple[str, ...]] = (
    "未確認",
    "指定されていない",
    "指定なし",
)


def _audio_intent(policy_json: str) -> bool:
    """True only where the policy ACTUALLY specifies sound/voice intent.

    ``scope.audio`` is the operator's own switch. The LLM ``audio_policy``
    text always carries a field — including the "未確認。…指定されていない。"
    unspecified boilerplate — so a bare substring match over the whole dump
    would route nearly every episode to audiovisual. A sentence counts as
    intent only if it mentions a term AND carries no negation marker.
    """

    try:
        document: object = json.loads(policy_json)
    except ValueError:
        lowered = policy_json.casefold()
        return any(term in lowered for term in AUDIO_POLICY_TERMS)
    if not isinstance(document, dict):
        return False
    scope = document.get("scope")
    if isinstance(scope, dict) and scope.get("audio") is True:
        return True
    audio_policy = document.get("audio_policy")
    if not isinstance(audio_policy, str) or not audio_policy:
        return False
    for sentence in audio_policy.split("。"):  # noqa: RUF001 (Japanese policy prose)
        if not sentence:
            continue
        lowered = sentence.casefold()
        if any(negation in sentence for negation in _NO_AUDIO_INTENT_MARKERS):
            continue
        if any(term in lowered for term in AUDIO_POLICY_TERMS):
            return True
    return False


def select_observation_route(
    *,
    override: RequestedRoute | None,
    has_transcript: bool,
    policy_text: str,
) -> ObservationRoute:
    """Resolve the observation route from existing data (pure).

    An explicit ``visual``/``audiovisual`` override always wins. ``auto``
    (or absent) yields ``audiovisual`` only when the episode has a speech
    transcript AND the adopted policy specifies sound/voice intent
    (scope.audio or a non-negated audio_policy sentence); otherwise
    ``visual``.
    """

    if override == "visual":
        return "visual"
    if override == "audiovisual":
        return "audiovisual"
    if has_transcript and _audio_intent(policy_text):
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
