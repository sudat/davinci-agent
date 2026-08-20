"""Measured-dimension parity comparisons (Todo 61): audio and color.

Audio duration/loudness/peak are compared under the DECLARED Todo-59
target window and tolerances from measured observations only (unmeasured
loudness fails closed); color metadata and SMPTE-bar region statistics
under the declared Todo-60 region tolerance. Every mismatch carries the
preview/final payload it was decided from.
"""

from __future__ import annotations

from services.presentation.parity_models import (
    AudioObservation,
    ColorObservation,
    ParityMismatch,
    ParityTolerances,
)


def _diff(preview: object, final: object) -> dict[str, str]:
    return {"preview": repr(preview), "final": repr(final)}


def _loudness_window_mismatches(
    preview: AudioObservation, final: AudioObservation, tolerances: ParityTolerances
) -> tuple[ParityMismatch, ...]:
    target = tolerances.audio_loudness_target_mlufs
    tolerance = tolerances.audio_loudness_tolerance_mlufs
    payload = {
        "preview": str(preview.integrated_mlufs),
        "final": str(final.integrated_mlufs),
        "target": str(target),
    }
    for label, observed in (("preview", preview), ("final", final)):
        value = observed.integrated_mlufs
        if value is None or not target - tolerance <= value <= target + tolerance:
            return (
                ParityMismatch(
                    code="unexplained_difference",
                    dim="audio_loudness",
                    detail=(
                        f"{label} integrated loudness "
                        + ("unmeasured" if value is None else str(value))
                        + " outside the declared target window (fail closed)"
                    ),
                    diff=payload,
                ),
            )
    if (
        preview.integrated_mlufs is not None
        and final.integrated_mlufs is not None
        and abs(preview.integrated_mlufs - final.integrated_mlufs) > tolerance
    ):
        return (
            ParityMismatch(
                code="unexplained_difference",
                dim="audio_loudness",
                detail="preview/final loudness drift beyond the declared tolerance",
                diff=payload | {"tolerance_mlufs": str(tolerance)},
            ),
        )
    return ()


def _peak_mismatches(
    preview: AudioObservation, final: AudioObservation, tolerances: ParityTolerances
) -> tuple[ParityMismatch, ...]:
    ceiling = tolerances.audio_max_peak_mb
    payload = {
        "preview": str(preview.peak_mb),
        "final": str(final.peak_mb),
        "ceiling_mb": str(ceiling),
    }
    for label, observed in (("preview", preview), ("final", final)):
        if observed.peak_mb > ceiling:
            return (
                ParityMismatch(
                    code="unexplained_difference",
                    dim="audio_peak",
                    detail=f"{label} measured peak exceeds the declared ceiling",
                    diff=payload,
                ),
            )
    if abs(preview.peak_mb - final.peak_mb) > tolerances.audio_peak_tolerance_mb:
        return (
            ParityMismatch(
                code="unexplained_difference",
                dim="audio_peak",
                detail="preview/final peak drift beyond the declared tolerance",
                diff=payload
                | {"tolerance_mb": str(tolerances.audio_peak_tolerance_mb)},
            ),
        )
    return ()


def compare_audio(
    preview: AudioObservation | None,
    final: AudioObservation | None,
    tolerances: ParityTolerances,
) -> tuple[ParityMismatch, ...]:
    if preview is None or final is None:
        return (
            ParityMismatch(
                code="unexplained_difference",
                dim="audio_duration",
                detail="one side carries no measured audio observation (fail closed)",
                diff=_diff(
                    "present" if preview else "missing",
                    "present" if final else "missing",
                ),
            ),
        )
    mismatches: list[ParityMismatch] = []
    drift = abs(preview.duration_samples - final.duration_samples)
    if drift > tolerances.audio_duration_tolerance_samples:
        mismatches.append(
            ParityMismatch(
                code="unexplained_difference",
                dim="audio_duration",
                detail="measured audio durations drift beyond the declared tolerance",
                diff={
                    "preview": str(preview.duration_samples),
                    "final": str(final.duration_samples),
                    "tolerance_samples": str(
                        tolerances.audio_duration_tolerance_samples
                    ),
                },
            )
        )
    mismatches.extend(_loudness_window_mismatches(preview, final, tolerances))
    mismatches.extend(_peak_mismatches(preview, final, tolerances))
    for label, observed in (("preview", preview), ("final", final)):
        if observed.channels != tolerances.audio_channels:
            mismatches.append(
                ParityMismatch(
                    code="unexplained_difference",
                    dim="audio_loudness",
                    detail=f"{label} channel count differs from the declared targets",
                    diff={
                        "preview": str(preview.channels),
                        "final": str(final.channels),
                        "declared": str(tolerances.audio_channels),
                    },
                )
            )
            break
    return tuple(mismatches)


def compare_color(
    preview: ColorObservation | None,
    final: ColorObservation | None,
    tolerance: int,
) -> tuple[ParityMismatch, ...]:
    if preview is None or final is None:
        return (
            ParityMismatch(
                code="unexplained_difference",
                dim="color_metadata",
                detail="one side carries no color observation (fail closed)",
                diff=_diff(
                    "present" if preview else "missing",
                    "present" if final else "missing",
                ),
            ),
        )
    mismatches: list[ParityMismatch] = []
    for field in ("color_space", "color_transfer", "color_primaries"):
        preview_value = getattr(preview, field)
        final_value = getattr(final, field)
        if preview_value != final_value and (preview_value or final_value):
            mismatches.append(
                ParityMismatch(
                    code="unexplained_difference",
                    dim="color_metadata",
                    detail=f"declared color metadata {field} differs where defined",
                    diff={"preview": preview_value, "final": final_value},
                )
            )
    for region in sorted(set(preview.region_means) | set(final.region_means)):
        preview_mean = preview.region_means.get(region)
        final_mean = final.region_means.get(region)
        if preview_mean is None or final_mean is None:
            mismatches.append(
                ParityMismatch(
                    code="unexplained_difference",
                    dim="color_statistics",
                    detail=f"region {region!r} measured on only one side",
                    diff={
                        "preview": (
                            str(preview_mean) if preview_mean is not None else "missing"
                        ),
                        "final": (
                            str(final_mean) if final_mean is not None else "missing"
                        ),
                    },
                )
            )
        elif abs(preview_mean - final_mean) > tolerance:
            mismatches.append(
                ParityMismatch(
                    code="unexplained_difference",
                    dim="color_statistics",
                    detail=(
                        f"region {region!r} luma means drift beyond "
                        "the declared tolerance"
                    ),
                    diff={
                        "preview": str(preview_mean),
                        "final": str(final_mean),
                        "tolerance": str(tolerance),
                    },
                )
            )
    return tuple(mismatches)



__all__ = ["compare_audio", "compare_color"]
