"""Preview/Final parity comparator (Todo 61): declared dims, typed verdicts.

Structural dims compare exactly; measured dims delegate to
:mod:`services.presentation.parity_measured`; shared derivatives must be
the SAME hash on both sides; a byte-equality-only basis is rejected.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Final

from services.presentation.color_render import LUMA_TOLERANCE
from services.presentation.parity_measured import compare_audio, compare_color
from services.presentation.parity_models import (
    ComparisonBasis,
    ParityDim,
    ParityMismatch,
    ParityReport,
    ParitySide,
    ParityTolerances,
)

if TYPE_CHECKING:
    from services.presentation.audio_models import AudioTargets

DEFAULT_DURATION_TOLERANCE_SAMPLES: Final = 4800
DEFAULT_PEAK_TOLERANCE_MB: Final = 200

DECLARED_PARITY_DIMS: Final[tuple[ParityDim, ...]] = (
    "manifest_anchor",
    "items",
    "assets",
    "cues",
    "placements",
    "subtitle_regions",
    "shared_derivatives",
    "audio_duration",
    "audio_loudness",
    "audio_peak",
    "color_metadata",
    "color_statistics",
)


class ParityError(Exception):
    """Typed parity failure; ``code`` is the machine cause."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def default_basis() -> ComparisonBasis:
    return ComparisonBasis(dims=DECLARED_PARITY_DIMS)


def tolerances_from_targets(
    targets: AudioTargets,
    *,
    audio_peak_tolerance_mb: int = DEFAULT_PEAK_TOLERANCE_MB,
    audio_duration_tolerance_samples: int = DEFAULT_DURATION_TOLERANCE_SAMPLES,
    color_region_tolerance: int = LUMA_TOLERANCE,
) -> ParityTolerances:
    """Declared tolerances reusing the Todo-59 targets and Todo-60 regions."""

    return ParityTolerances(
        audio_duration_tolerance_samples=audio_duration_tolerance_samples,
        audio_loudness_target_mlufs=targets.loudness_target_mlufs,
        audio_loudness_tolerance_mlufs=targets.loudness_tolerance_mlufs,
        audio_max_peak_mb=targets.max_peak_mb,
        audio_peak_tolerance_mb=audio_peak_tolerance_mb,
        audio_channels=targets.channels,
        color_region_tolerance=color_region_tolerance,
    )


def validate_comparison_basis(basis: ComparisonBasis) -> None:
    """Reject invalid comparison bases; byte equality alone proves nothing."""

    if basis.container_bytes_only:
        raise ParityError(
            "invalid_comparison_basis",
            "container byte equality is not a parity basis; compare the declared "
            "dimensions under the declared tolerances",
        )
    declared = {dim for dim in basis.dims if dim != "manifest_anchor"}
    if not declared:
        raise ParityError(
            "invalid_comparison_basis",
            "a comparison basis must declare at least one compared dimension",
        )


def _diff(preview: object, final: object) -> dict[str, str]:
    return {"preview": repr(preview), "final": repr(final)}


def _structural(
    dim: ParityDim, preview: object, final: object
) -> tuple[ParityMismatch, ...]:
    if preview == final:
        return ()
    return (
        ParityMismatch(
            code="unexplained_difference",
            dim=dim,
            detail=f"{dim} differ between preview and final builds",
            diff=_diff(preview, final),
        ),
    )


def _compare_derivatives(
    preview: ParitySide, final: ParitySide
) -> tuple[tuple[ParityMismatch, ...], dict[str, str]]:
    mismatches: list[ParityMismatch] = []
    shared: dict[str, str] = {}
    for kind in sorted(set(preview.derivatives) | set(final.derivatives)):
        preview_sha = preview.derivatives.get(kind)
        final_sha = final.derivatives.get(kind)
        if preview_sha is None or final_sha is None:
            mismatches.append(
                ParityMismatch(
                    code="missing_shared_derivative",
                    dim="shared_derivatives",
                    detail=f"declared shared derivative {kind!r} missing on one side",
                    diff={
                        "preview": preview_sha or "missing",
                        "final": final_sha or "missing",
                    },
                )
            )
        elif preview_sha != final_sha:
            mismatches.append(
                ParityMismatch(
                    code="derivative_hash_mismatch",
                    dim="shared_derivatives",
                    detail=f"preview and final consume different {kind!r} derivatives",
                    diff={"preview": preview_sha, "final": final_sha},
                )
            )
        else:
            shared[kind] = preview_sha
    return tuple(mismatches), shared


def _cue_text_projection(side: ParitySide) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (cue.item_id, cue.text, cue.lines, cue.start_frame, cue.end_frame)
        for cue in side.cues
    )


def _cue_region_projection(side: ParitySide) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            cue.item_id,
            cue.style_id,
            cue.font_size_px,
            cue.margin_bottom_px,
            cue.primary_color_hex,
        )
        for cue in side.cues
    )


def _structural_dims(
    preview: ParitySide, final: ParitySide, active: frozenset[ParityDim]
) -> tuple[ParityMismatch, ...]:
    projections: tuple[tuple[ParityDim, Callable[[ParitySide], object]], ...] = (
        ("items", lambda side: side.items),
        ("assets", lambda side: side.assets),
        ("placements", lambda side: side.placements),
        ("cues", _cue_text_projection),
        ("subtitle_regions", _cue_region_projection),
    )
    mismatches: list[ParityMismatch] = []
    if "manifest_anchor" in active:
        mismatches.extend(
            _structural(
                "manifest_anchor",
                (preview.manifest_sha256, preview.profile_snapshot_sha256),
                (final.manifest_sha256, final.profile_snapshot_sha256),
            )
        )
    for dim, project in projections:
        if dim in active:
            mismatches.extend(_structural(dim, project(preview), project(final)))
    return tuple(mismatches)


def compare_parity(
    preview: ParitySide,
    final: ParitySide,
    tolerances: ParityTolerances,
    basis: ComparisonBasis | None = None,
) -> ParityReport:
    """Compare preview vs final along the declared dims; typed mismatches."""

    declared = basis if basis is not None else default_basis()
    try:
        validate_comparison_basis(declared)
    except ParityError as error:
        return ParityReport(
            manifest_sha256=preview.manifest_sha256,
            basis=declared,
            mismatches=(
                ParityMismatch(code="invalid_comparison_basis", detail=str(error)),
            ),
            passed=False,
        )
    selected: tuple[ParityDim, ...] = tuple(
        dim for dim in DECLARED_PARITY_DIMS if dim in frozenset(declared.dims)
    )
    active = frozenset[ParityDim](selected)
    mismatches: list[ParityMismatch] = []
    mismatches.extend(_structural_dims(preview, final, active))
    shared: dict[str, str] = {}
    if "shared_derivatives" in active:
        derivative_mismatches, shared = _compare_derivatives(preview, final)
        mismatches.extend(derivative_mismatches)
    if active & {"audio_duration", "audio_loudness", "audio_peak"}:
        mismatches.extend(compare_audio(preview.audio, final.audio, tolerances))
    if active & {"color_metadata", "color_statistics"}:
        mismatches.extend(
            compare_color(
                preview.color, final.color, tolerances.color_region_tolerance
            )
        )
    ordered = tuple(sorted(mismatches, key=lambda m: (m.dim or "", m.code, m.detail)))
    return ParityReport(
        manifest_sha256=preview.manifest_sha256,
        basis=declared,
        dims_compared=selected,
        derivative_hashes=shared,
        mismatches=ordered,
        passed=not ordered,
    )


__all__ = [
    "DECLARED_PARITY_DIMS",
    "DEFAULT_DURATION_TOLERANCE_SAMPLES",
    "DEFAULT_PEAK_TOLERANCE_MB",
    "ParityError",
    "compare_parity",
    "default_basis",
    "tolerances_from_targets",
    "validate_comparison_basis",
]
