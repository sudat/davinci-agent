"""Error taxonomy for normalization; every refusal carries a stable label."""

from __future__ import annotations

from typing import Literal

VerificationReason = Literal[
    "stale_source_manifest",
    "source_mutated",
    "undecodable_output",
    "silent_metadata_loss",
    "unexplained_frame_count",
]


class NormalizeError(Exception):
    """Base class; ``label`` is the stable machine-readable reason."""

    label = "normalize_error"


class NormalizeToolDriftError(NormalizeError):
    """A pinned binary no longer matches the frozen toolchain lock."""

    label = "toolchain_hash_drift"


class NormalizeRecipeError(NormalizeError):
    """Recipe/target lookup or validation failed (never retried)."""

    label = "recipe_rejected"


class NormalizeOnlineRelinkError(NormalizeError):
    """Recipe argv referenced a network location; refused pre-execution."""

    label = "online_relink_refused"


class NormalizeExecutionError(NormalizeError):
    """The pinned ffmpeg run failed or exceeded its bounded timeout."""

    label = "ffmpeg_execution_failed"


class NormalizeVerificationError(NormalizeError):
    """Output failed post-verification; committed nothing."""

    label = "output_verification_failed"

    def __init__(self, reason_code: VerificationReason, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


class NormalizeRecordError(NormalizeError):
    """A NormalizeRecord failed validation (e.g. output/source hash confusion)."""

    label = "record_invalid"
