"""Strict artifact models and error taxonomy for dialogue-audio analysis.

Canonical fields never use floats: amplitudes are integer sample units or
milli-decibels (``mb``), loudness is milli-LUFS (``mlu``), confidences are
integer permille 0-1000, and spans are half-open integer SAMPLE spans with a
floor-derived millisecond echo. Dialogue and Ambient measurements live in
strictly separate fields and can never be conflated into one mixed field.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.edit_plan_0c import EditPlan0C
from services.contracts.primitives import Sha256, StrictModel


class AnalyzeError(Exception):
    """Base analyzer error; carries a machine-readable label."""

    label: str = "analyze_error"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.label}: {self.detail}"


class AnalyzeRequestError(AnalyzeError):
    """Refused before execution: malformed request, Edit Plan input, drift."""

    label = "analyze_request_refused"


class AnalyzeToolDriftError(AnalyzeError):
    """A pinned binary no longer matches the frozen toolchain lock."""

    label = "analyze_tool_drift"


class AnalyzeTimeBaseError(AnalyzeError):
    """Declared sample rate disagrees with the probed stream (anti-confusion)."""

    label = "analyze_time_base_mismatch"


class AnalyzeDecodeError(AnalyzeError):
    """The Edit-Source audio failed bounded decode validation."""

    label = "corrupt_decode"

    def __init__(self, detail: str, probe: AudioProbeArtifact | None = None) -> None:
        super().__init__(detail)
        self.probe = probe


def refuse_edit_plan(value: object) -> None:
    """Analyzers are read-only Artifact producers; Edit Plans are refused."""

    if isinstance(value, EditPlan0C):
        raise AnalyzeRequestError(
            "analyzers accept artifact references only: an Edit Plan was passed"
        )


class StreamFacts(StrictModel):
    sample_rate: Literal[16000, 48000]
    channels: Literal[1] = 1
    codec: Literal["pcm_s16le"] = "pcm_s16le"


class SampleMsSpan(StrictModel):
    """Half-open [start, end) sample span with its floor-derived ms echo."""

    start_sample: int = Field(ge=0, strict=True)
    end_sample: int = Field(ge=0, strict=True)
    sample_rate: int = Field(gt=0, strict=True)
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def require_forward_and_consistent(self) -> SampleMsSpan:
        if self.end_sample < self.start_sample:
            raise PydanticCustomError("span_inverted", "end_sample before start_sample")
        if self.start_ms != self.start_sample * 1000 // self.sample_rate:
            raise PydanticCustomError("ms_inconsistent", "start_ms must floor-match start_sample")
        if self.end_ms != self.end_sample * 1000 // self.sample_rate:
            raise PydanticCustomError("ms_inconsistent", "end_ms must floor-match end_sample")
        return self

    @property
    def length(self) -> int:
        return self.end_sample - self.start_sample


class WindowStat(StrictModel):
    """One full measurement window: [start_sample, end_sample) of raw PCM."""

    start_sample: int = Field(ge=0, strict=True)
    end_sample: int = Field(gt=0, strict=True)
    mean_square: int = Field(ge=0, strict=True)
    rms_mb: int = Field(le=0, strict=True)


class LoudnessSummary(StrictModel):
    """Program loudness. ``ebur128`` is the real ITU-R BS.1770 measurement
    from the pinned ffmpeg filter; ``rms_fallback`` is an RMS approximation
    and is labeled as NOT BS.1770 — it never claims the standard."""

    method: Literal["ebur128", "rms_fallback"]
    honest_label: Literal["itu_r_bs_1770_ebur128", "rms_based_not_bs1770"]
    integrated_loudness_mlufs: int | None
    rms_mean_mb: int = Field(le=0, strict=True)

    @model_validator(mode="after")
    def require_method_consistency(self) -> LoudnessSummary:
        if self.method == "ebur128":
            if self.integrated_loudness_mlufs is None:
                raise PydanticCustomError("loudness_missing", "ebur128 requires integrated mLU")
            if self.honest_label != "itu_r_bs_1770_ebur128":
                raise PydanticCustomError("loudness_mislabeled", "ebur128 label mismatch")
        elif self.integrated_loudness_mlufs is not None:
            raise PydanticCustomError(
                "loudness_mislabeled", "rms fallback must not claim an integrated LUFS value"
            )
        return self


class DialogueAmbientSummary(StrictModel):
    """Dialogue (speech spans) and Ambient (silence spans) kept strictly
    separate; a single mixed field is a validation error by construction."""

    dialogue_rms_mb: int = Field(le=0, strict=True)
    ambient_noise_floor_mb: int = Field(le=0, strict=True)
    dialogue_sample_count: int = Field(ge=0, strict=True)
    ambient_sample_count: int = Field(ge=0, strict=True)


class ProbeFailure(StrictModel):
    code: Literal["corrupt_decode", "probe_failed", "time_base_mismatch", "stream_facts_mismatch"]
    detail: str


class AudioProbeArtifact(StrictModel):
    """Decode-validation outcome with stream facts and tool hashes."""

    decode_ok: bool
    facts: StreamFacts | None = None
    failure: ProbeFailure | None = None
    ffprobe_sha256: Sha256
    ffmpeg_sha256: Sha256
    decode_argv: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def require_ok_xor_failure(self) -> AudioProbeArtifact:
        if self.decode_ok != (self.failure is None):
            raise PydanticCustomError(
                "probe_state_mismatch", "decode_ok and failure must be exclusive"
            )
        if self.decode_ok and self.facts is None:
            raise PydanticCustomError("probe_facts_missing", "a clean probe carries stream facts")
        return self


class WavBinding(StrictModel):
    wav_path: str
    wav_sha256: Sha256
    declared: StreamFacts


class AudioMeasurements(StrictModel):
    window_ms: int = Field(gt=0, strict=True)
    hop_ms: int = Field(gt=0, strict=True)
    sample_rate: int = Field(gt=0, strict=True)
    frame_count: int = Field(ge=0, strict=True)
    peak_sample: int = Field(ge=0, strict=True)
    peak_mb: int = Field(le=0, strict=True)
    clipping_count: int = Field(ge=0, strict=True)
    window_stats: tuple[WindowStat, ...]
    silence_spans: tuple[SampleMsSpan, ...]
    loudness: LoudnessSummary
