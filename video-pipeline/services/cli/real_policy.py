"""The real-episode resolved production policy snapshot (Todo 46).

The chain always writes ``resolved-policy.json`` into its out dir: the
LOCAL-ONLY posture (no cloud classes) with the ``review_translate`` stage's
``review_instruction_text`` class declared, so ``services.cli.review
propose`` works out of the box against the emitted bundle. Going LIVE for
the editorial director requires the operator's PRD §8 grant
(``EpisodeEditorialGrantV1`` persisted as
``<episode-dir>/editorial-grant.json`` via the cockpit grant API):
``policy_for_grant`` adds ``transcript`` at ``editorial_direct`` for that
episode ONLY — deny-by-default; the bare default snapshot intentionally
does not.

``video_understanding_policy`` (v44 video-understanding, T3) is the
representative episode's explicit cloud-data declaration for the corrected
Arm B: video+audio+transcript may leave for the Gemini moment-review stage
and original_video ONLY for the audio-stripped specialist stage
(``audio`` is deliberately NOT granted there).
"""

from __future__ import annotations

from pathlib import Path

from services.config.models import (
    BudgetPolicy,
    CloudAllowlistEntry,
    EpisodeConfig,
    NetworkPosture,
    PathAllowlist,
    ResolvedConfig,
    RetentionPolicy,
    StageDataClasses,
    SystemConfig,
)
from services.config.resolver import resolve
from services.episode_cockpit.models import EpisodeEditorialGrantV1
from services.foundation_io import atomic_write, canonical_model_bytes

POLICY_NAME = "resolved-policy.json"
VIDEO_UNDERSTANDING_POLICY_NAME = "resolved-policy-video-understanding.json"
#: The persisted operator grant the runner/chain consult (episode metadata,
#: mirroring ``intake.json``): explicit file > nothing — no env, no default.
GRANT_FILE_NAME = "editorial-grant.json"

MOMENT_REVIEW_STAGE = "moment_review"
MOMENT_REVIEW_SPECIALIST_STAGE = "moment_review_specialist"


def local_only_policy(episode_id: str) -> ResolvedConfig:
    system = SystemConfig(
        schema_version="system-config-v1",
        retention=RetentionPolicy(authoritative="permanent", rebuildable_days=30),
        data_classes=(
            StageDataClasses(stage="review_translate", classes=("review_instruction_text",)),
        ),
        cloud_allowlist=(),
        network=NetworkPosture(
            builder="loopback", builder_endpoint="unix:///run/davinci-agent/editorial.sock"
        ),
        path_allowlist=PathAllowlist(roots=("/video-pipeline/jobs",)),
        budget=BudgetPolicy(
            transient_max_attempts=3,
            permanent_max_attempts=1,
            blocking_human_max_attempts=1,
            max_stage_cost_units=1000,
            max_job_cost_units=10000,
        ),
    )
    return resolve(system, episode=EpisodeConfig(episode_id=episode_id))


def video_understanding_policy(episode_id: str) -> ResolvedConfig:
    """Cloud-data declaration for the corrected Arm B video understanding.

    Gemini (lead/reduce/fusion) may receive original_video, audio, and
    transcript at ``moment_review``; the GLM specialist may receive
    original_video ONLY at ``moment_review_specialist`` — audio is never
    granted to the specialist stage, which is how the audio-stripped rule
    stays machine-checkable through ``authorize_cloud_transport``.
    """

    local = local_only_policy(episode_id)
    system = SystemConfig(
        schema_version="system-config-v1",
        retention=local.retention,
        data_classes=(
            StageDataClasses(stage="review_translate", classes=("review_instruction_text",)),
            StageDataClasses(
                stage=MOMENT_REVIEW_STAGE, classes=("audio", "original_video", "transcript")
            ),
            StageDataClasses(stage=MOMENT_REVIEW_SPECIALIST_STAGE, classes=("original_video",)),
        ),
        cloud_allowlist=(
            CloudAllowlistEntry(
                data_class="original_video", stage=MOMENT_REVIEW_STAGE, fixture_only=False
            ),
            CloudAllowlistEntry(data_class="audio", stage=MOMENT_REVIEW_STAGE, fixture_only=False),
            CloudAllowlistEntry(
                data_class="transcript", stage=MOMENT_REVIEW_STAGE, fixture_only=False
            ),
            CloudAllowlistEntry(
                data_class="original_video",
                stage=MOMENT_REVIEW_SPECIALIST_STAGE,
                fixture_only=False,
            ),
        ),
        network=local.network,
        path_allowlist=local.path_allowlist,
        budget=local.budget,
    )
    return resolve(system, episode=EpisodeConfig(episode_id=episode_id))


EDITORIAL_DIRECT_STAGE = "editorial_direct"
EDITORIAL_DIRECT_DATA_CLASS = "transcript"


def granted_policy(episode_id: str) -> ResolvedConfig:
    """The operator-granted snapshot: local-only PLUS transcript@editorial_direct.

    Built ONLY from a persisted ``EpisodeEditorialGrantV1`` with
    ``granted=True`` (see ``policy_for_grant``) — never from a default.
    The allowlist carries exactly the ONE grantable pair; frames, audio,
    and every other stage stay local_only.
    """

    local = local_only_policy(episode_id)
    system = SystemConfig(
        schema_version="system-config-v1",
        retention=local.retention,
        data_classes=(
            StageDataClasses(stage="review_translate", classes=("review_instruction_text",)),
            StageDataClasses(
                stage=EDITORIAL_DIRECT_STAGE, classes=(EDITORIAL_DIRECT_DATA_CLASS,)
            ),
        ),
        cloud_allowlist=(
            CloudAllowlistEntry(
                data_class=EDITORIAL_DIRECT_DATA_CLASS,
                stage=EDITORIAL_DIRECT_STAGE,
                fixture_only=False,
            ),
        ),
        network=local.network,
        path_allowlist=local.path_allowlist,
        budget=local.budget,
    )
    return resolve(system, episode=EpisodeConfig(episode_id=episode_id))


def policy_for_grant(
    episode_id: str, grant: EpisodeEditorialGrantV1 | None
) -> ResolvedConfig:
    """Resolve the director snapshot for one episode's persisted grant.

    ``granted=True`` with the exact transcript→editorial_direct scope
    yields the granted snapshot; anything else (never declared, revoked
    ``granted=False``, or a scope that cannot name the grantable pair)
    yields the local-only snapshot — deny-by-default, no silent widen.
    """

    if (
        grant is not None
        and grant.granted
        and grant.episode_id == episode_id
        and grant.data_class == EDITORIAL_DIRECT_DATA_CLASS
        and grant.stage == EDITORIAL_DIRECT_STAGE
    ):
        return granted_policy(episode_id)
    return local_only_policy(episode_id)


def load_episode_grant(episode_root: Path) -> EpisodeEditorialGrantV1 | None:
    """Read the episode's persisted operator grant; None = never declared."""

    path = episode_root / GRANT_FILE_NAME
    if not path.is_file():
        return None
    return EpisodeEditorialGrantV1.model_validate_json(path.read_bytes())


def write_policy_snapshot(
    episode_id: str, out_dir: Path, editorial_grant: EpisodeEditorialGrantV1 | None = None
) -> Path:
    path = out_dir / POLICY_NAME
    atomic_write(path, canonical_model_bytes(policy_for_grant(episode_id, editorial_grant)))
    return path


def write_video_understanding_policy_snapshot(episode_id: str, out_dir: Path) -> Path:
    path = out_dir / VIDEO_UNDERSTANDING_POLICY_NAME
    atomic_write(path, canonical_model_bytes(video_understanding_policy(episode_id)))
    return path


def load_policy(path: Path) -> ResolvedConfig:
    return ResolvedConfig.model_validate_json(path.read_bytes())


__all__ = [
    "MOMENT_REVIEW_SPECIALIST_STAGE",
    "MOMENT_REVIEW_STAGE",
    "POLICY_NAME",
    "VIDEO_UNDERSTANDING_POLICY_NAME",
    "granted_policy",
    "load_episode_grant",
    "load_policy",
    "local_only_policy",
    "policy_for_grant",
    "video_understanding_policy",
    "write_policy_snapshot",
    "write_video_understanding_policy_snapshot",
]
