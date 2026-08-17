"""Cloud transport authorization: deny-by-default, local is the default (PRD 27).

Transcript/OCR/text data classes carry untrusted media-derived content, so they
are only ever transportable when the resolved allowlist explicitly grants the
exact data class AND stage, and — when the grant is ``fixture_only`` — the
episode is one of the frozen synthetic fixture ids. Everything else stays
``local_only``. Decisions are recomputed from the resolved snapshot on every
call; nothing is cached or trusted from a previous decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import Field

from services.contracts.primitives import StrictModel
from services.fixtures.manifest_phase0c import PHASE_0C_FIXTURE_IDS

if TYPE_CHECKING:
    from services.config.models import DataClass, ResolvedConfig

SYNTHETIC_FIXTURE_EPISODE_IDS: frozenset[str] = frozenset(PHASE_0C_FIXTURE_IDS)


class CloudTransportDecision(StrictModel):
    decision: Literal["allow", "deny"]
    reason: str = Field(min_length=1)

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


def authorize_cloud_transport(
    resolved: ResolvedConfig,
    *,
    data_class: DataClass,
    stage: str,
    episode_id: str,
) -> CloudTransportDecision:
    """Authorize one (data_class, stage, episode) cloud transport request."""

    for entry in resolved.cloud_allowlist:
        if entry.data_class != data_class or entry.stage != stage:
            continue
        if entry.fixture_only and episode_id not in SYNTHETIC_FIXTURE_EPISODE_IDS:
            return CloudTransportDecision(
                decision="deny",
                reason=(
                    f"fixture-only cloud permission for {data_class} at stage {stage} does "
                    f"not cover episode '{episode_id}'; the default is local_only"
                ),
            )
        scope = " (synthetic fixtures only)" if entry.fixture_only else ""
        return CloudTransportDecision(
            decision="allow",
            reason=(
                f"allowlist grants {data_class} at stage {stage} for episode "
                f"'{episode_id}'{scope}"
            ),
        )
    return CloudTransportDecision(
        decision="deny",
        reason=(
            f"no cloud permission for data class '{data_class}' at stage '{stage}'; "
            f"episode '{episode_id}' stays local_only"
        ),
    )


__all__ = [
    "SYNTHETIC_FIXTURE_EPISODE_IDS",
    "CloudTransportDecision",
    "authorize_cloud_transport",
]
