"""Deny-by-default Spike authorization for the phase-0C Review Translator.

SUNSET (SPIKE_ALLOWLIST_SUNSET_NOTE): this adapter-local allowlist exists only
because the post-0C Control Plane policy service was not built yet. It may
authorize ONLY the frozen synthetic phase-0C Cloud fixtures, never Production
Episode data. As of Todo 12 the episode-level transport decision is DELEGATED
to the Control Plane successor ``services.policy.data_policy`` (via a minimal
resolved configuration in this module); this adapter now only adds the frozen
toolchain-pin bindings (policy profile + schema version) on top, and its
deny-by-default outcome is unchanged.

What the frozen pin actually freezes for the translator
(``config/toolchains/phase-0c-v2.json`` → ``preview_review``):

- ``review_translator_policy_profile_id`` = ``phase-0c-deterministic-classifier-v1``
  (the translator policy version this adapter binds),
- ``schema_version`` = ``preview-review-v1``,
- ``external_model`` = ``none`` — no concrete external model ID is pinned for
  phase 0C; the concrete production model pin arrives with the Phase-1
  editorial-model freeze (Todo 32),
- ``adapter`` = ``pinned-ffmpeg-preview``.

The pin freezes no episode id, so the synthetic Cloud fixture binding is the
frozen phase-0C fixture id set itself (the only synthetic data this Spike may
send toward a Cloud model).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from services.config.models import (
    BudgetPolicy,
    CloudAllowlistEntry,
    DataClass,
    EpisodeConfig,
    NetworkPosture,
    PathAllowlist,
    ResolvedConfig,
    RetentionPolicy,
    StageDataClasses,
    SystemConfig,
)
from services.config.resolver import resolve
from services.contracts.primitives import StrictModel
from services.fixtures.manifest_phase0c import PHASE_0C_FIXTURE_IDS
from services.policy.data_policy import authorize_cloud_transport

SPIKE_ALLOWLIST_SUNSET_NOTE = (
    "SPIKE AUTHORITY: the episode-level decision is delegated to Control Plane "
    "policy (services.policy.data_policy, Todo 12); only the frozen toolchain-pin "
    "bindings remain adapter-local. It may authorize ONLY the frozen synthetic "
    "phase-0C Cloud fixtures, never Production Episode data."
)
DEFAULT_TOOLCHAIN_LOCK = Path("config/toolchains/phase-0c-v2.json")
SYNTHETIC_FIXTURE_EPISODE_IDS: frozenset[str] = frozenset(PHASE_0C_FIXTURE_IDS)
TRANSLATOR_DATA_CLASS: DataClass = "review_instruction_text"
TRANSLATOR_STAGE = "review_translate"


def _resolved_translator_policy() -> ResolvedConfig:
    """Minimal Control-Plane policy view for this Spike: fixture-only text."""
    system = SystemConfig(
        schema_version="system-config-v1",
        retention=RetentionPolicy(authoritative="permanent", rebuildable_days=30),
        data_classes=(
            StageDataClasses(stage=TRANSLATOR_STAGE, classes=(TRANSLATOR_DATA_CLASS,)),
        ),
        cloud_allowlist=(
            CloudAllowlistEntry(
                data_class=TRANSLATOR_DATA_CLASS, stage=TRANSLATOR_STAGE, fixture_only=True
            ),
        ),
        network=NetworkPosture(
            builder="loopback", builder_endpoint="unix:///run/davinci-agent/translator.sock"
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
    return resolve(
        system, episode=EpisodeConfig(episode_id="phase-0c-translator-spike")
    )


class PolicySourceError(Exception):
    """The frozen toolchain pin is missing, unreadable, or malformed."""


class PreviewReviewPin(StrictModel):
    # ignores the pin's unrelated `smoke` block; reads only translator-frozen fields
    model_config = StrictModel.model_config | {"extra": "ignore"}

    adapter: str
    external_model: str
    review_translator_policy_profile_id: str
    schema_version: str


class FrozenTranslatorContract(StrictModel):
    policy_profile_id: str
    preview_schema_version: str
    external_model: str
    adapter: str


def load_frozen_contract(lock_path: Path = DEFAULT_TOOLCHAIN_LOCK) -> FrozenTranslatorContract:
    """Load the translator fields the phase-0C pin freezes; fail closed."""

    try:
        document: object = json.loads(lock_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise PolicySourceError(f"toolchain lock {lock_path} is not a JSON object")
        preview = document.get("preview_review")
        pin = PreviewReviewPin.model_validate(preview)
    except (OSError, ValidationError, ValueError) as error:
        message = f"cannot read frozen translator contract from {lock_path}: {error}"
        raise PolicySourceError(message) from error
    return FrozenTranslatorContract(
        policy_profile_id=pin.review_translator_policy_profile_id,
        preview_schema_version=pin.schema_version,
        external_model=pin.external_model,
        adapter=pin.adapter,
    )


def authorize(
    *,
    episode_id: str,
    policy_profile_id: str,
    schema_version: str,
    contract: FrozenTranslatorContract,
) -> str | None:
    """Return a denial reason unless the request binds the frozen synthetic fixture.

    Deny-by-default. DELEGATION (Todo 12): the episode-level transport decision
    is delegated to ``services.policy.data_policy.authorize_cloud_transport``
    over this Spike's fixture-only resolved policy; only the frozen toolchain
    bindings (policy profile, schema version) are still checked here. Every
    mismatch is reported; ``None`` means the request is granted. This never
    authorizes Production Episode data.
    """

    reasons: list[str] = []
    decision = authorize_cloud_transport(
        _resolved_translator_policy(),
        data_class=TRANSLATOR_DATA_CLASS,
        stage=TRANSLATOR_STAGE,
        episode_id=episode_id,
    )
    if not decision.allowed:
        reasons.append(decision.reason)
    if policy_profile_id != contract.policy_profile_id:
        reasons.append(
            f"policy profile '{policy_profile_id}' does not match the frozen translator "
            f"policy profile '{contract.policy_profile_id}'"
        )
    if schema_version != contract.preview_schema_version:
        reasons.append(
            f"schema version '{schema_version}' does not match the frozen preview schema "
            f"version '{contract.preview_schema_version}'"
        )
    if reasons:
        return "; ".join(reasons)
    return None
