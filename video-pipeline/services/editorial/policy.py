"""Deny-by-default data-policy envelope for the Editorial Director stage.

SUNSET (editorial adaptation of the Todo-29 translator pattern): the episode
level decision is DELEGATED to the Control Plane policy
``services.policy.data_policy.authorize_cloud_transport`` (Todo 12) over this
stage's minimal resolved configuration. The Todo-12 synthetic-fixture set
currently enumerates only the frozen phase-0C Cloud fixture ids, while this
stage's frozen synthetic episodes are the five Phase-1 references; the
adapter therefore adds the FROZEN TOOLCHAIN FIXTURE BINDING — only
``PHASE_1_FIXTURE_IDS`` episodes may leave the local lane at all — which
narrows the delegate grant to exactly the frozen synthetic set. Production
episodes stay ``local_only`` (replay/offline) until a Control Plane policy
grant covers them; that authorization does not exist in this repository.
"""

from __future__ import annotations

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
from services.editorial.models import EditorialPolicyEnvelope
from services.fixtures.manifest_phase1 import PHASE_1_FIXTURE_IDS
from services.policy.data_policy import authorize_cloud_transport

EDITORIAL_STAGE = "editorial_direct"
EDITORIAL_DATA_CLASS: DataClass = "transcript"
EDITORIAL_POLICY_PROFILE_ID = "phase-1-editorial-director-policy-v1"

_FIXTURE_BINDING_GRANTED = (
    "frozen toolchain fixture binding granted: episode is one of the five frozen "
    "Phase-1 synthetic editorial fixtures"
)
_FIXTURE_BINDING_DENIED = (
    "episode is not one of the five frozen Phase-1 synthetic editorial fixtures; "
    "the editorial stage stays local_only (replay/offline) and no cloud transport "
    "is attempted"
)


def _resolved_policy() -> ResolvedConfig:
    """Minimal Control-Plane view for this stage; see the module sunset note."""

    system = SystemConfig(
        schema_version="system-config-v1",
        retention=RetentionPolicy(authoritative="permanent", rebuildable_days=30),
        data_classes=(
            StageDataClasses(stage=EDITORIAL_STAGE, classes=(EDITORIAL_DATA_CLASS,)),
        ),
        cloud_allowlist=(
            CloudAllowlistEntry(
                data_class=EDITORIAL_DATA_CLASS, stage=EDITORIAL_STAGE, fixture_only=False
            ),
        ),
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
    return resolve(system, episode=EpisodeConfig(episode_id="phase-1-editorial-spike"))


def decide_transport_policy(episode_id: str) -> EditorialPolicyEnvelope:
    """Decide the data-policy envelope; deny-by-default, recomputed per call."""

    granted = episode_id in PHASE_1_FIXTURE_IDS
    decision = authorize_cloud_transport(
        _resolved_policy(),
        data_class=EDITORIAL_DATA_CLASS,
        stage=EDITORIAL_STAGE,
        episode_id=episode_id,
    )
    return EditorialPolicyEnvelope(
        fixture_binding="granted" if granted else "denied",
        binding_reason=_FIXTURE_BINDING_GRANTED if granted else _FIXTURE_BINDING_DENIED,
        control_plane_decision="allow" if decision.allowed else "deny",
        control_plane_reason=decision.reason,
    )


def decide_production_transport_policy(
    episode_id: str, policy: ResolvedConfig
) -> EditorialPolicyEnvelope:
    """The production-path envelope: the resolved production policy decides.

    The episode is by definition NOT a fixture here, so the frozen fixture
    binding stays honestly ``denied``; the granting authority is the Todo-12
    gate over the operator's resolved production snapshot, deny-by-default.
    """

    decision = authorize_cloud_transport(
        policy,
        data_class=EDITORIAL_DATA_CLASS,
        stage=EDITORIAL_STAGE,
        episode_id=episode_id,
    )
    return EditorialPolicyEnvelope(
        fixture_binding="denied",
        binding_reason=(
            "real episode: the frozen toolchain fixture binding does not apply; "
            "transport authorization is decided by the resolved production policy"
        ),
        control_plane_decision="allow" if decision.allowed else "deny",
        control_plane_reason=decision.reason,
        binding_scope="production-policy",
    )


__all__ = [
    "EDITORIAL_DATA_CLASS",
    "EDITORIAL_POLICY_PROFILE_ID",
    "EDITORIAL_STAGE",
    "decide_production_transport_policy",
    "decide_transport_policy",
]
