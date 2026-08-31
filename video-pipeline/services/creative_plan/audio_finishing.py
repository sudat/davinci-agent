"""AudioFinishingPlanV1 — task 34; PRD §10.3, impl-plan §8.4.

The semantic audio finishing LADDER as typed stages in fixed order:
dialogue cleanup / noise handling -> dialogue level normalization ->
optional eq/compression/voice isolation -> BGM placement -> music ducking
-> ambience preservation -> optional SFX -> loudness/peak QC. The plan
stores SEMANTIC goals and target ranges only — no Fairlight/API/execution
detail anywhere (Resolve/MCP execution is compiled later in tasks 38/39;
the only mcp-fit knowledge here is acceptance STATUS metadata gating the
accepted-only ops).

Guards enforced AT THE MODEL (hand-built plans are checked too):
- the stage sequence is exactly the ladder, in order, whatever the enables;
- a disabled stage or op requires a justification (no silent no-ops);
- ops live only on the optional processing stage, whose ``enabled`` must
  match ``any(op.enabled)``;
- target ranges carry a metric whose unit pairing is coherent, and each
  stage carries only its own metrics (QC carries BOTH loudness and peak).

Guards enforced by :func:`validate_audio_finishing` (they need external
state the model must not fetch):
- already-good audio guard — with ``dialogue_clean AND measured_loudness_ok``
  facts, an ENABLED cleanup/normalization stage or processing op requires an
  explicit justification (improving good audio is forbidden unless a
  semantic reason is supplied);
- dialogue-only satisfiability guard — with ``no BGM AND no ambience``
  facts, an enabled normalization + QC pair must carry INTERSECTING LUFS
  ranges (both gates measure the same full-render integrated loudness, so
  disjoint ranges are an impossible contract);
- capability gate — an enabled accepted-only op bound to a non-accepted
  mcp-fit capability row requires an explicit justification.

Auto-justification vocabulary is a stable contract ("already-good:",
"not-present:", "not-requested:") — downstream tooling matches on the
prefixes.
"""

# allow: SIZE_OK — pure ladder model + its table-driven builder/guard; the
# plan pins everything to audio_finishing.py and the per-stage tables are
# irreducible (one row per ladder stage). Task-23/29/31 single-module
# precedent.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, StrictModel, to_tuple
from services.toolchain.mcp_fit import load_mcp_fit

DEFAULT_MCP_FIT_PATH = (
    Path(__file__).resolve().parents[2] / "capabilities" / "v4.3" / "mcp-fit.json"
)

AudioStageName = Literal[
    "dialogue_cleanup",
    "dialogue_level_normalization",
    "optional_eq_compression_voice_isolation",
    "bgm_placement",
    "music_ducking",
    "ambience_preservation",
    "optional_sfx",
    "loudness_peak_qc",
]

AUDIO_LADDER: tuple[AudioStageName, ...] = (
    "dialogue_cleanup",
    "dialogue_level_normalization",
    "optional_eq_compression_voice_isolation",
    "bgm_placement",
    "music_ducking",
    "ambience_preservation",
    "optional_sfx",
    "loudness_peak_qc",
)

AudioProcessingOpName = Literal["eq", "compression", "voice_isolation"]

# Accepted-only ops bind to mcp-fit capability rows by id (status metadata
# only — never execution detail).
OP_CAPABILITY: dict[AudioProcessingOpName, str] = {
    "eq": "audio-property-operation",
    "compression": "audio-property-operation",
    "voice_isolation": "voice-isolation",
}

AudioMetric = Literal[
    "noise_reduction",
    "dialogue_loudness",
    "bgm_level",
    "duck_depth",
    "ambience_level",
    "sfx_peak",
    "integrated_loudness",
    "true_peak",
]

AudioUnit = Literal["db", "dbfs", "dbtp", "lufs"]

_METRIC_UNITS: dict[AudioMetric, AudioUnit] = {
    "noise_reduction": "db",
    "dialogue_loudness": "lufs",
    "bgm_level": "lufs",
    "duck_depth": "db",
    "ambience_level": "db",
    "sfx_peak": "dbfs",
    "integrated_loudness": "lufs",
    "true_peak": "dbtp",
}

_STAGE_GOALS: dict[AudioStageName, str] = {
    "dialogue_cleanup": "remove background noise and restore clean dialogue",
    "dialogue_level_normalization": "bring dialogue loudness into the channel target range",
    "optional_eq_compression_voice_isolation": "shape the voice only when accepted and useful",
    "bgm_placement": "place background music beneath the dialogue",
    "music_ducking": "duck music around dialogue so words stay intelligible",
    "ambience_preservation": "preserve natural ambience under the edit",
    "optional_sfx": "accent selected moments with sound effects",
    "loudness_peak_qc": "verify integrated loudness and true peak against delivery targets",
}

_STAGE_METRICS: dict[AudioStageName, frozenset[AudioMetric]] = {
    "dialogue_cleanup": frozenset({"noise_reduction"}),
    "dialogue_level_normalization": frozenset({"dialogue_loudness"}),
    "optional_eq_compression_voice_isolation": frozenset({"dialogue_loudness"}),
    "bgm_placement": frozenset({"bgm_level"}),
    "music_ducking": frozenset({"duck_depth"}),
    "ambience_preservation": frozenset({"ambience_level"}),
    "optional_sfx": frozenset({"sfx_peak"}),
    "loudness_peak_qc": frozenset({"integrated_loudness", "true_peak"}),
}

# Stable auto-justification prefixes — a vocabulary contract, not prose.
_ALREADY_GOOD = "already-good: measured dialogue and loudness are within target"
_NOT_PRESENT = "not-present: episode facts contain no material for this stage"
_NOT_REQUESTED = "not-requested: kept disabled absent an explicit request"


class AudioFinishingError(ValueError):
    """Typed refusal from the audio finishing builder/guard (never silent)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class UnnecessaryAudioProcessingError(AudioFinishingError):
    """Enabled stage/op on already-good audio without a semantic reason."""


class UnacceptedAudioCapabilityError(AudioFinishingError):
    """Enabled accepted-only op bound to a non-accepted capability row."""


class UnknownAudioCapabilityError(AudioFinishingError):
    """Op capability id absent from the capability status map."""


class TargetRangeV1(StrictModel):
    """One semantic target range — metric, closed numeric span, unit."""

    metric: AudioMetric
    minimum: float
    maximum: float
    unit: AudioUnit

    @model_validator(mode="after")
    def require_coherent_range(self) -> TargetRangeV1:
        if self.minimum > self.maximum:
            raise PydanticCustomError("range_inverted", "minimum must not exceed maximum")
        if self.unit != _METRIC_UNITS[self.metric]:
            raise PydanticCustomError(
                "metric_unit_mismatch",
                "metric {metric} must use unit {unit}",
                {"metric": self.metric, "unit": _METRIC_UNITS[self.metric]},
            )
        return self


_Justification = Annotated[str, Field(min_length=1, strict=True)] | None


class AudioProcessingOpV1(StrictModel):
    """One accepted-only processing op; capability ref is status metadata."""

    op: AudioProcessingOpName
    capability: str
    accepted_only: Literal[True] = True
    enabled: bool = False
    justification: _Justification = None

    @model_validator(mode="after")
    def require_coherent_op(self) -> AudioProcessingOpV1:
        if self.capability != OP_CAPABILITY[self.op]:
            raise PydanticCustomError(
                "op_capability_mismatch",
                "op {op} must bind capability {capability}",
                {"op": self.op, "capability": OP_CAPABILITY[self.op]},
            )
        if not self.enabled and self.justification is None:
            raise PydanticCustomError(
                "noop_justification_required",
                "disabled op {op} requires a justification",
                {"op": self.op},
            )
        return self


class AudioFinishingStageV1(StrictModel):
    """One ladder stage: semantic goal, target ranges, enable, justification."""

    stage: AudioStageName
    goal: Annotated[str, Field(min_length=1, strict=True)]
    target_ranges: Annotated[
        tuple[TargetRangeV1, ...], BeforeValidator(to_tuple)
    ] = Field(min_length=1)
    enabled: bool
    justification: _Justification = None
    ops: Annotated[tuple[AudioProcessingOpV1, ...], BeforeValidator(to_tuple)] = ()

    @model_validator(mode="after")
    def require_coherent_stage(self) -> AudioFinishingStageV1:
        allowed = _STAGE_METRICS[self.stage]
        for target in self.target_ranges:
            if target.metric not in allowed:
                raise PydanticCustomError(
                    "stage_metric_mismatch",
                    "stage {stage} must carry metrics {metrics}",
                    {"stage": self.stage, "metrics": sorted(allowed)},
                )
        if self.stage == "loudness_peak_qc" and {
            target.metric for target in self.target_ranges
        } != allowed:
            raise PydanticCustomError(
                "qc_ranges_incomplete", "loudness_peak_qc needs loudness AND peak ranges"
            )
        if self.stage != "loudness_peak_qc" and len(self.target_ranges) != 1:
            raise PydanticCustomError(
                "stage_range_count",
                "stage {stage} carries exactly one range",
                {"stage": self.stage},
            )
        if self.ops and self.stage != "optional_eq_compression_voice_isolation":
            raise PydanticCustomError(
                "ops_outside_optional_stage", "processing ops live only on the optional stage"
            )
        if self.stage == "optional_eq_compression_voice_isolation" and self.enabled != any(
            op.enabled for op in self.ops
        ):
            raise PydanticCustomError(
                "optional_stage_enable_mismatch",
                "optional stage enabled must match any op enabled",
            )
        if not self.enabled and self.justification is None:
            raise PydanticCustomError(
                "noop_justification_required",
                "disabled stage {stage} requires a justification",
                {"stage": self.stage},
            )
        return self


class AudioFinishingPlanV1(StrictModel):
    """The audio finishing ladder as a committed-plan model (PRD §10.3)."""

    schema_version: Literal["audio-finishing-plan-v1"]
    episode_id: Identifier
    stages: Annotated[tuple[AudioFinishingStageV1, ...], BeforeValidator(to_tuple)]

    @model_validator(mode="after")
    def require_ladder(self) -> AudioFinishingPlanV1:
        if [stage.stage for stage in self.stages] != list(AUDIO_LADDER):
            raise PydanticCustomError(
                "ladder_order",
                "stages must be exactly the audio ladder in fixed order: {ladder}",
                {"ladder": list(AUDIO_LADDER)},
            )
        return self


class AudioFactsV1(StrictModel):
    """Measured facts the plan is built from — the already-good guard input."""

    episode_id: Identifier
    dialogue_clean: bool
    has_bgm: bool
    has_ambience: bool
    measured_loudness_ok: bool


class AudioOpRequestV1(StrictModel):
    """An editorial request to enable one processing op."""

    op: AudioProcessingOpName
    justification: _Justification = None


class AudioFinishingPolicyV1(StrictModel):
    """Channel audio policy: one target range per ladder metric."""

    noise_reduction_db: TargetRangeV1
    dialogue_loudness_lufs: TargetRangeV1
    bgm_level_lufs: TargetRangeV1
    duck_depth_db: TargetRangeV1
    ambience_level_db: TargetRangeV1
    sfx_peak_dbfs: TargetRangeV1
    integrated_loudness_lufs: TargetRangeV1
    true_peak_dbtp: TargetRangeV1

    @model_validator(mode="after")
    def require_field_metrics(self) -> AudioFinishingPolicyV1:
        for field_name, metric in _POLICY_FIELD_METRICS.items():
            if getattr(self, field_name).metric != metric:
                raise PydanticCustomError(
                    "policy_metric_mismatch",
                    "policy field {field} must carry metric {metric}",
                    {"field": field_name, "metric": metric},
                )
        return self


_POLICY_FIELD_METRICS: dict[str, AudioMetric] = {
    "noise_reduction_db": "noise_reduction",
    "dialogue_loudness_lufs": "dialogue_loudness",
    "bgm_level_lufs": "bgm_level",
    "duck_depth_db": "duck_depth",
    "ambience_level_db": "ambience_level",
    "sfx_peak_dbfs": "sfx_peak",
    "integrated_loudness_lufs": "integrated_loudness",
    "true_peak_dbtp": "true_peak",
}

DEFAULT_AUDIO_POLICY = AudioFinishingPolicyV1(
    noise_reduction_db=TargetRangeV1(
        metric="noise_reduction", minimum=3.0, maximum=12.0, unit="db"
    ),
    dialogue_loudness_lufs=TargetRangeV1(
        metric="dialogue_loudness", minimum=-18.0, maximum=-16.0, unit="lufs"
    ),
    bgm_level_lufs=TargetRangeV1(metric="bgm_level", minimum=-30.0, maximum=-25.0, unit="lufs"),
    duck_depth_db=TargetRangeV1(metric="duck_depth", minimum=6.0, maximum=12.0, unit="db"),
    ambience_level_db=TargetRangeV1(
        metric="ambience_level", minimum=-36.0, maximum=-30.0, unit="db"
    ),
    sfx_peak_dbfs=TargetRangeV1(metric="sfx_peak", minimum=-12.0, maximum=-6.0, unit="dbfs"),
    integrated_loudness_lufs=TargetRangeV1(
        metric="integrated_loudness", minimum=-14.5, maximum=-13.5, unit="lufs"
    ),
    true_peak_dbtp=TargetRangeV1(metric="true_peak", minimum=-2.0, maximum=-1.0, unit="dbtp"),
)

_STAGE_POLICY_FIELDS: dict[AudioStageName, tuple[str, ...]] = {
    "dialogue_cleanup": ("noise_reduction_db",),
    "dialogue_level_normalization": ("dialogue_loudness_lufs",),
    "optional_eq_compression_voice_isolation": ("dialogue_loudness_lufs",),
    "bgm_placement": ("bgm_level_lufs",),
    "music_ducking": ("duck_depth_db",),
    "ambience_preservation": ("ambience_level_db",),
    "optional_sfx": ("sfx_peak_dbfs",),
    "loudness_peak_qc": ("integrated_loudness_lufs", "true_peak_dbtp"),
}


def load_capability_statuses(path: Path | None = None) -> dict[str, str]:
    """Capability id -> acceptance status from the mcp-fit matrix."""
    data = load_mcp_fit(path if path is not None else DEFAULT_MCP_FIT_PATH)
    return {row["capability"]: row["status"] for row in data["capabilities"]}


def _auto_justification(stage: AudioStageName, facts: AudioFactsV1) -> str | None:
    """Why a fact-disabled stage is off; None means the facts enable it."""
    match stage:
        case "dialogue_cleanup":
            return _ALREADY_GOOD if facts.dialogue_clean else None
        case "dialogue_level_normalization":
            return _ALREADY_GOOD if facts.measured_loudness_ok else None
        case "bgm_placement" | "music_ducking":
            return None if facts.has_bgm else _NOT_PRESENT
        case "ambience_preservation":
            return None if facts.has_ambience else _NOT_PRESENT
        case "optional_sfx":
            return _NOT_REQUESTED
        case "optional_eq_compression_voice_isolation" | "loudness_peak_qc":
            return None


def build_audio_plan(
    audio_facts: AudioFactsV1,
    *,
    policy: AudioFinishingPolicyV1,
    op_requests: Sequence[AudioOpRequestV1] = (),
    capability_statuses: Mapping[str, str] | None = None,
) -> AudioFinishingPlanV1:
    """Build the ladder plan from facts + policy; every disable is justified.

    Range semantics are facts-aware: on a dialogue-only episode (no BGM,
    no ambience) the normalization stage carries the policy's DELIVERY
    target under the ``dialogue_loudness`` metric — dialogue loudness and
    whole-program loudness are the same measured quantity there, so the
    dialogue-domain policy range would create two disjoint gates over one
    render. Episodes with BGM/ambience keep the dialogue-domain range.

    Raises :class:`AudioFinishingError` subclasses for duplicate op requests,
    unnecessary processing on already-good audio, and enabled ops bound to
    non-accepted capability rows without an explicit justification.
    """
    requests: dict[AudioProcessingOpName, AudioOpRequestV1] = {}
    for request in op_requests:
        if request.op in requests:
            raise AudioFinishingError(
                "duplicate-op-request", f"op {request.op} requested more than once"
            )
        requests[request.op] = request
    ops = tuple(
        AudioProcessingOpV1(
            op=name,
            capability=capability,
            enabled=name in requests,
            justification=(
                requests[name].justification if name in requests else _NOT_REQUESTED
            ),
        )
        for name, capability in OP_CAPABILITY.items()
    )
    stages = []
    for stage in AUDIO_LADDER:
        if stage == "optional_eq_compression_voice_isolation":
            enabled = any(op.enabled for op in ops)
            justification = None if enabled else _NOT_REQUESTED
            stage_model = AudioFinishingStageV1(
                stage=stage,
                goal=_STAGE_GOALS[stage],
                target_ranges=_stage_ranges(stage, policy, audio_facts),
                enabled=enabled,
                justification=justification,
                ops=ops,
            )
        else:
            auto = _auto_justification(stage, audio_facts)
            stage_model = AudioFinishingStageV1(
                stage=stage,
                goal=_STAGE_GOALS[stage],
                target_ranges=_stage_ranges(stage, policy, audio_facts),
                enabled=auto is None,
                justification=auto,
            )
        stages.append(stage_model)
    plan = AudioFinishingPlanV1(
        schema_version="audio-finishing-plan-v1",
        episode_id=audio_facts.episode_id,
        stages=tuple(stages),
    )
    statuses = (
        dict(capability_statuses)
        if capability_statuses is not None
        else load_capability_statuses()
    )
    return validate_audio_finishing(
        plan, audio_facts=audio_facts, capability_statuses=statuses
    )


def _require_satisfiable_dialogue_only_loudness(
    stages: Mapping[str, AudioFinishingStageV1], facts: AudioFactsV1
) -> None:
    """Dialogue-only satisfiability guard: with no BGM and no ambience the
    normalization gate and the delivery gate measure the SAME full-render
    integrated loudness — an enabled pair with disjoint LUFS ranges is an
    impossible contract and must never reach execution."""
    if facts.has_bgm or facts.has_ambience:
        return
    normalization = stages["dialogue_level_normalization"]
    qc = stages["loudness_peak_qc"]
    if not (normalization.enabled and qc.enabled):
        return
    qc_loudness = next(
        target for target in qc.target_ranges if target.metric == "integrated_loudness"
    )
    target = normalization.target_ranges[0]
    if not _ranges_intersect(target, qc_loudness):
        raise AudioFinishingError(
            "dialogue-only-loudness-conflict",
            f"a dialogue-only program measures one integrated loudness, but "
            f"dialogue_level_normalization [{target.minimum}, {target.maximum}] and "
            f"loudness_peak_qc [{qc_loudness.minimum}, {qc_loudness.maximum}] are "
            "disjoint — no render can satisfy both",
        )


def _stage_ranges(
    stage: AudioStageName, policy: AudioFinishingPolicyV1, facts: AudioFactsV1
) -> tuple[TargetRangeV1, ...]:
    if (
        stage == "dialogue_level_normalization"
        and not facts.has_bgm
        and not facts.has_ambience
    ):
        # Dialogue-only program: the dialogue IS the whole program, so the
        # dialogue-loudness gate and the delivery gate measure the same
        # render — carrying two disjoint ranges there would be physically
        # unsatisfiable. The dialogue gate therefore equals the delivery
        # target; both remain independently measured from fresh renders.
        delivery = policy.integrated_loudness_lufs
        return (
            TargetRangeV1(
                metric="dialogue_loudness",
                minimum=delivery.minimum,
                maximum=delivery.maximum,
                unit="lufs",
            ),
        )
    return tuple(getattr(policy, field) for field in _STAGE_POLICY_FIELDS[stage])


def _ranges_intersect(a: TargetRangeV1, b: TargetRangeV1) -> bool:
    return max(a.minimum, b.minimum) <= min(a.maximum, b.maximum)


def validate_audio_finishing(
    plan: AudioFinishingPlanV1,
    *,
    audio_facts: AudioFactsV1,
    capability_statuses: Mapping[str, str],
) -> AudioFinishingPlanV1:
    """Guard a plan against facts and capability acceptance statuses."""
    if plan.episode_id != audio_facts.episode_id:
        raise AudioFinishingError(
            "episode-mismatch",
            f"plan episode {plan.episode_id} != facts episode {audio_facts.episode_id}",
        )
    stages = {stage.stage: stage for stage in plan.stages}
    _require_satisfiable_dialogue_only_loudness(stages, audio_facts)
    audio_already_good = audio_facts.dialogue_clean and audio_facts.measured_loudness_ok
    if audio_already_good:
        for name in ("dialogue_cleanup", "dialogue_level_normalization"):
            stage = stages[name]
            if stage.enabled and stage.justification is None:
                raise UnnecessaryAudioProcessingError(
                    "unnecessary-processing",
                    f"{name} enabled on already-good audio without justification",
                )
    for op in stages["optional_eq_compression_voice_isolation"].ops:
        if not op.enabled:
            continue
        if audio_already_good and op.justification is None:
            raise UnnecessaryAudioProcessingError(
                "unnecessary-processing",
                f"op {op.op} enabled on already-good audio without justification",
            )
        status = capability_statuses.get(op.capability)
        if status is None:
            raise UnknownAudioCapabilityError(
                "unknown-capability", f"capability {op.capability} absent from status map"
            )
        if status != "accepted" and op.justification is None:
            raise UnacceptedAudioCapabilityError(
                "unaccepted-capability",
                f"op {op.op} requires justification: capability "
                f"{op.capability} status is {status!r}",
            )
    return plan


__all__ = [
    "AUDIO_LADDER",
    "DEFAULT_AUDIO_POLICY",
    "DEFAULT_MCP_FIT_PATH",
    "OP_CAPABILITY",
    "AudioFactsV1",
    "AudioFinishingError",
    "AudioFinishingPlanV1",
    "AudioFinishingPolicyV1",
    "AudioFinishingStageV1",
    "AudioMetric",
    "AudioOpRequestV1",
    "AudioProcessingOpName",
    "AudioProcessingOpV1",
    "AudioStageName",
    "AudioUnit",
    "TargetRangeV1",
    "UnacceptedAudioCapabilityError",
    "UnknownAudioCapabilityError",
    "UnnecessaryAudioProcessingError",
    "build_audio_plan",
    "load_capability_statuses",
    "validate_audio_finishing",
]
