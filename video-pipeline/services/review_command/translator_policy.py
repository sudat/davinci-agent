"""Deny-by-default Spike authorization for the phase-0C Review Translator.

SUNSET (SPIKE_ALLOWLIST_SUNSET_NOTE): this adapter-local allowlist exists only
because the post-0C Control Plane policy service is not built yet. It may
authorize ONLY the frozen synthetic phase-0C Cloud fixtures, never Production
Episode data, and it stops being the authority once Control Plane policy lands
with Todo 12; it must then be removed in favor of that policy service.

What the frozen pin actually freezes for the translator
(``config/toolchains/phase-0c-v1.json`` → ``preview_review``):

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

from services.contracts.primitives import StrictModel
from services.fixtures.manifest_phase0c import PHASE_0C_FIXTURE_IDS

SPIKE_ALLOWLIST_SUNSET_NOTE = (
    "TEMPORARY SPIKE AUTHORITY: this adapter-local allowlist exists only before the "
    "post-0C Control Plane exists. It may authorize ONLY the frozen synthetic "
    "phase-0C Cloud fixtures, never Production Episode data, and is superseded by "
    "Control Plane policy after Todo 12, at which point it must be removed."
)
DEFAULT_TOOLCHAIN_LOCK = Path("config/toolchains/phase-0c-v1.json")
SYNTHETIC_FIXTURE_EPISODE_IDS: frozenset[str] = frozenset(PHASE_0C_FIXTURE_IDS)


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

    Deny-by-default: every mismatch (episode, policy profile, schema version)
    is reported; ``None`` means the Spike allowlist grants the request. This
    never authorizes Production Episode data.
    """

    reasons: list[str] = []
    if episode_id not in SYNTHETIC_FIXTURE_EPISODE_IDS:
        reasons.append(
            f"episode '{episode_id}' is not one of the frozen synthetic phase-0C Cloud fixtures"
        )
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
