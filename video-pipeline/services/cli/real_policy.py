"""The real-episode resolved production policy snapshot (Todo 46).

The chain always writes ``resolved-policy.json`` into its out dir: the
LOCAL-ONLY posture (no cloud classes) with the ``review_translate`` stage's
``review_instruction_text`` class declared, so ``services.cli.review
propose`` works out of the box against the emitted bundle. Going LIVE for
the editorial director requires an operator-supplied snapshot that
additionally grants ``transcript`` at ``editorial_direct`` for the episode —
deny-by-default; this default snapshot intentionally does not.
"""

from __future__ import annotations

from pathlib import Path

from services.config.models import (
    BudgetPolicy,
    EpisodeConfig,
    NetworkPosture,
    PathAllowlist,
    ResolvedConfig,
    RetentionPolicy,
    StageDataClasses,
    SystemConfig,
)
from services.config.resolver import resolve
from services.foundation_io import atomic_write, canonical_model_bytes

POLICY_NAME = "resolved-policy.json"


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


def write_policy_snapshot(episode_id: str, out_dir: Path) -> Path:
    path = out_dir / POLICY_NAME
    atomic_write(path, canonical_model_bytes(local_only_policy(episode_id)))
    return path


def load_policy(path: Path) -> ResolvedConfig:
    return ResolvedConfig.model_validate_json(path.read_bytes())


__all__ = ["POLICY_NAME", "load_policy", "local_only_policy", "write_policy_snapshot"]
