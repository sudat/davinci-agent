"""windows=None routed observation: visual vs audiovisual (PRD v4.4 §8.5).

Route selection uses existing data only (explicit override wins;
otherwise transcript presence + audio-relevant adopted policy). The
audiovisual route additionally needs the episode's AV consent: an
explicit request without consent fails typed (no silent GLM
substitution); an ``auto`` request without consent — or whose AV call
fails or proves insufficient — falls back to the visual route while
the observation record marks audio integration unconfirmed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from services.episode_cockpit.av_observation_server import resolve_av_sample_windows
from services.episode_cockpit.consultation_store import latest_adopted_policy
from services.episode_cockpit.sample_observation_server import (
    episode_has_transcript,
    resolve_server_sample_windows,
)
from services.media_intelligence.observation_route import (
    RequestedRoute,
    read_av_consent,
    select_observation_route,
)
from services.media_intelligence.sample_observation import SampleObservationError

if TYPE_CHECKING:
    from services.contracts.primitives import RecordFrameSpan
    from services.contracts.timeline_ir import TimelineIr0C


def _with_route_fields(
    observation_json: str, *, route: str, audio_unconfirmed: str | None
) -> str:
    record: dict[str, Any] = json.loads(observation_json)
    record["route"] = route
    if audio_unconfirmed is not None:
        record["audio_integration"] = "unconfirmed"
        record["route_fallback_note"] = audio_unconfirmed
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def resolve_routed_sample_windows(
    full_ir: TimelineIr0C,
    episode_dir: Path,
    episode_id: str,
    *,
    route_override: RequestedRoute | None = None,
) -> tuple[list[RecordFrameSpan], str]:
    """Select the observation route and resolve widened sample windows."""

    policy = latest_adopted_policy(episode_dir)
    route = select_observation_route(
        override=route_override,
        has_transcript=episode_has_transcript(episode_dir),
        policy_text=policy.model_dump_json() if policy is not None else "",
    )
    explicit = route_override in ("visual", "audiovisual")
    if route == "visual":
        windows, observation = resolve_server_sample_windows(
            full_ir, episode_dir, episode_id
        )
        return list(windows), _with_route_fields(
            observation, route="visual", audio_unconfirmed=None
        )
    if not read_av_consent(episode_dir, episode_id):
        if explicit:
            raise SampleObservationError(
                "sample-av-consent-missing",
                "the explicitly requested audiovisual observation needs the "
                "episode's AV cloud-send consent (av-analysis-consent.json); "
                "refusing rather than substituting a visual-only observation",
            )
        windows, observation = resolve_server_sample_windows(
            full_ir, episode_dir, episode_id
        )
        return list(windows), _with_route_fields(
            observation,
            route="visual",
            audio_unconfirmed="auto route needed audiovisual evidence but the "
            "episode carries no AV consent; visual-only observation",
        )
    try:
        return resolve_av_sample_windows(full_ir, episode_dir, episode_id)
    except SampleObservationError as error:
        if explicit:
            raise
        windows, observation = resolve_server_sample_windows(
            full_ir, episode_dir, episode_id
        )
        return list(windows), _with_route_fields(
            observation,
            route="visual",
            audio_unconfirmed=f"auto audiovisual observation failed ({error.code}); "
            "visual-only observation",
        )


__all__ = ["resolve_routed_sample_windows"]
