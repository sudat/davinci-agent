"""Minimal Supported Episode eligibility (PRD 94-137).

``classify_episode`` reads ONLY declared source-manifest fields. It never
scans media, transcripts, or OCR content, and it performs NO automated
Privacy/Rights detection: manually declared flags are passed through as
structured human-gate blockers. Todo 38 expands this routing seam.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field

from services.contracts.primitives import StrictModel

MAX_SOURCE_DURATION_SEC = 5400  # PRD 3.1: total source material budget (90 min)
MAX_SEPARABLE_SPEAKERS = 4

type FlagSequence = Annotated[tuple[str, ...], BeforeValidator(tuple)]


class EligibilityDeclared(StrictModel):
    language: str = Field(min_length=2)
    principal_video_count: int = Field(ge=0, strict=True)
    audio_present: bool
    vfr: bool
    cfr_normalizable: bool
    total_duration_sec: int = Field(gt=0, strict=True)
    speaker_count: int = Field(ge=1, strict=True)
    privacy_flags: FlagSequence = ()
    rights_flags: FlagSequence = ()


class EpisodeEligibilityBundle(StrictModel):
    episode_id: str = Field(min_length=1)
    contract_id: Literal["talking-head-mvp-v1"] = "talking-head-mvp-v1"
    declared: EligibilityDeclared


class EligibilityReason(StrictModel):
    code: str
    detail: str


class HumanGate(StrictModel):
    kind: Literal["privacy", "rights"]
    flag: str


class EligibilityResult(StrictModel):
    episode_id: str
    status: Literal["supported", "assisted", "unsupported"]
    contract_id: str
    reasons: tuple[EligibilityReason, ...]
    human_gates: tuple[HumanGate, ...]


def classify_episode(bundle: EpisodeEligibilityBundle) -> EligibilityResult:
    declared = bundle.declared
    reasons: list[EligibilityReason] = []
    gates = [HumanGate(kind="privacy", flag=flag) for flag in declared.privacy_flags] + [
        HumanGate(kind="rights", flag=flag) for flag in declared.rights_flags
    ]
    if not declared.audio_present:
        reasons.append(EligibilityReason(code="audio-absent", detail="no audio stream declared"))
    if declared.principal_video_count == 0:
        reasons.append(EligibilityReason(code="video-absent", detail="no principal video declared"))
    if declared.vfr and not declared.cfr_normalizable:
        reasons.append(
            EligibilityReason(
                code="vfr-unnormalizable",
                detail="VFR source declared non-normalizable to a CFR mezzanine",
            )
        )
    if [item.code for item in reasons]:
        return EligibilityResult(
            episode_id=bundle.episode_id,
            status="unsupported",
            contract_id=bundle.contract_id,
            reasons=tuple(reasons),
            human_gates=tuple(gates),
        )
    if declared.principal_video_count > 1:
        reasons.append(
            EligibilityReason(
                code="multi-principal-video",
                detail="single talking-head principal video required by the contract",
            )
        )
    if declared.total_duration_sec > MAX_SOURCE_DURATION_SEC:
        reasons.append(
            EligibilityReason(
                code="source-duration-over-budget",
                detail="total source material exceeds the 90-minute contract budget",
            )
        )
    if declared.speaker_count > MAX_SEPARABLE_SPEAKERS:
        reasons.append(
            EligibilityReason(
                code="speakers-above-contract",
                detail="more speakers than the separable few the contract allows",
            )
        )
    if declared.language != "ja":
        reasons.append(
            EligibilityReason(
                code="language-out-of-contract",
                detail="MVP contract language is Japanese",
            )
        )
    status: Literal["supported", "assisted", "unsupported"] = (
        "supported" if not reasons else "assisted"
    )
    return EligibilityResult(
        episode_id=bundle.episode_id,
        status=status,
        contract_id=bundle.contract_id,
        reasons=tuple(reasons),
        human_gates=tuple(gates),
    )
