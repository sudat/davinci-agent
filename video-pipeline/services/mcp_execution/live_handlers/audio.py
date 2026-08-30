"""Live audio-domain handlers: voice isolation + the Task 5 dialogue stages.

Voice isolation is idempotent: a preflight ``get_voice_isolation_state``
that already shows the exact desired state (enabled/60) is reused with
zero set calls; otherwise ONE set is verified through an independent get
(a set timeout is reconciled by readback, never a second set). Task 5
stages are wired through the pinned MCP only: ``audio/dialogue-chain``
resolves to ONE named Fairlight preset (``get_fairlight_presets`` must
list it; a missing preset is a permanent typed refusal — no API authors
presets) applied via ``apply_fairlight_preset`` with stage values
MEDIA-MEASURED from before/after Resolve renders, never plan echoes
(cleanup = loudness-relative noise-floor improvement, so a pure-gain
preset measures 0 dB; normalization = after-render integrated LUFS;
``render_boundary_report`` is never a mutation substitute). Out-of-range
output raises ``audio-stage-out-of-range`` with ``retryable=False`` — a
deterministic verdict, not a transient fault.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import uuid4

from pydantic import ValidationError

from services.mcp_client.errors import McpTimeoutError
from services.mcp_client.ops_models import (
    ApplyFairlightPresetResult,
    FairlightPresetsReadback,
    McpActionOutcome,
    RenderBoundaryReport,
    VoiceIsolationStateReadback,
)
from services.mcp_execution.audio_measurement import MeasuredAudio, parse_measured
from services.mcp_execution.live_handlers.audio_render import render_fresh_audio
from services.mcp_execution.live_handlers.common import (
    LiveAdapterError,
    LiveAdapterUnsupportedError,
    LiveSessionContext,
    require_ok,
    validate_params,
)
from services.mcp_execution.plan_payloads import AudioStageParams, VoiceIsolationParams
from services.qc.checks.audio_checks import AudioMeasurementError
from services.qc.tools import QcToolError


@dataclass(frozen=True)
class FairlightPresetBinding:
    """One kit selection → concrete named Fairlight preset identity."""

    preset_name: str
    kit_recipe_id: str


#: Operation-specific bounded deadline for the heavy voice-isolation
#: set/get pair. Live focused runs showed set_voice_isolation_state
#: completing on the server but timing out on the short transport default,
#: and the immediate get also timed out before the queued operation
#: finished. 120 s is the measured upper bound for this pair.
VOICE_ISOLATION_TIMEOUT_SECONDS: Final = 120.0

#: preset_ref → binding. The operator saves the named mix once in the
#: Fairlight UI (measured: no scripting API authors presets, and Resolve
#: 21.0.4.5 lists zero until then); a binding whose preset is absent from
#: the live listing refuses typed preset-missing, never a guessed success.
_FAIRLIGHT_PRESET_BINDINGS: Final[Mapping[str, FairlightPresetBinding]] = {
    "fairlight-dialogue-chain-v1": FairlightPresetBinding(
        preset_name="dialogue-chain",
        kit_recipe_id="audio/dialogue-chain",
    ),
}


def _is_desired_voice_state(cur: VoiceIsolationStateReadback) -> bool:
    return cur.is_enabled is True and cur.amount == 60  # noqa: PLR2004


def _read_voice_state(ctx: LiveSessionContext) -> VoiceIsolationStateReadback:
    cur = VoiceIsolationStateReadback.model_validate(
        ctx.transport(
            "timeline",
            "get_voice_isolation_state",
            {"track_index": 1},
            timeout_seconds=VOICE_ISOLATION_TIMEOUT_SECONDS,
        )
    )
    require_ok(cur, "get-voice")
    if cur.is_enabled is None or cur.amount is None:
        # Malformed readback (ok envelope, missing state fields) is a typed
        # failure — never a reason to fall through to a mutation.
        raise LiveAdapterError("voice-readback-incomplete", f"got {cur.is_enabled}/{cur.amount}")
    return cur


def _voice_success(p: VoiceIsolationParams, cur: VoiceIsolationStateReadback) -> dict[str, object]:
    return {
        "item_ref": str(p.target_item_id) if p.target_item_id else "timeline",
        "state_property": "voice_isolation",
        "is_enabled": cur.is_enabled,
        "amount": cur.amount,
    }


def set_voice_isolation_state(
    ctx: LiveSessionContext, _action: str, params: Mapping[str, object]
) -> dict[str, object]:
    p = validate_params(VoiceIsolationParams, params)
    # Preflight: reuse the exact desired state with zero set calls.
    cur = _read_voice_state(ctx)
    if _is_desired_voice_state(cur):
        return _voice_success(p, cur)
    set_error: BaseException | None = None
    try:
        require_ok(
            McpActionOutcome.model_validate(
                ctx.transport(
                    "timeline",
                    "set_voice_isolation_state",
                    {"state": {"isEnabled": True, "amount": 60}, "track_index": 1},
                    timeout_seconds=VOICE_ISOLATION_TIMEOUT_SECONDS,
                )
            ),
            "set-voice",
        )
        ctx.mark_timeline_mutated()
    except McpTimeoutError as exc:
        # Set was sent exactly once; a lost response is reconciled through
        # an independent readback — never a second set.
        set_error = exc
    cur = _read_voice_state(ctx)
    if not _is_desired_voice_state(cur):
        raise LiveAdapterError(
            "voice-readback-mismatch", f"got {cur.is_enabled}/{cur.amount} after set"
        ) from set_error
    if set_error is not None:
        ctx.mark_timeline_mutated()
    return _voice_success(p, cur)


def _measure(ctx: LiveSessionContext, media: Path) -> MeasuredAudio:
    if ctx.audio_measure is None:
        raise LiveAdapterError(
            "measurement-unavailable", "no rendered-media measurement port is wired"
        )
    try:
        payload = ctx.audio_measure(str(media))
        return parse_measured(payload, media)
    except ValidationError as error:
        raise LiveAdapterError("measurement-invalid", str(error)) from error
    except (AudioMeasurementError, QcToolError, OSError) as error:
        raise LiveAdapterError(
            "measurement-unavailable", f"measuring {media.name} failed: {error}"
        ) from error


def _preset_binding(params: AudioStageParams) -> FairlightPresetBinding:
    binding = _FAIRLIGHT_PRESET_BINDINGS.get(params.preset_ref or "")
    if binding is None:
        raise LiveAdapterUnsupportedError(
            "preset-binding-unknown",
            f"stage {params.stage!r} preset_ref {params.preset_ref!r} has no "
            "dialogue-chain Fairlight preset binding",
        )
    return binding


def _render_and_measure(ctx: LiveSessionContext, name: str) -> MeasuredAudio:
    """Measure one fresh probe render, then delete the rebuildable file.

    The measurement is hash-bound to the media BEFORE deletion, so the
    returned evidence survives the unlink. A failed deletion raises
    visibly — a silent leak is exactly the reported incident (dozens of
    ~450 MB probe renders per live run)."""
    media = render_fresh_audio(ctx, name)
    try:
        return _measure(ctx, media)
    finally:
        media.unlink(missing_ok=True)


def apply_dialogue_preset(
    ctx: LiveSessionContext, _action: str, params: Mapping[str, object]
) -> dict[str, object]:
    p = validate_params(AudioStageParams, params)
    binding = _preset_binding(p)
    if ctx.audio_measure is None:
        raise LiveAdapterError(
            "measurement-unavailable", "no rendered-media measurement port is wired"
        )
    listing = FairlightPresetsReadback.model_validate(
        ctx.transport("resolve_control", "get_fairlight_presets", {})
    )
    require_ok(listing, "fairlight-preset-list")
    if binding.preset_name not in listing.presets:
        raise LiveAdapterUnsupportedError(
            "preset-missing",
            f"Fairlight preset {binding.preset_name!r} (kit {binding.kit_recipe_id}) "
            f"is absent from the {len(listing.presets)} presets Resolve lists; save "
            "it in the Fairlight UI once — success is never guessed",
        )
    token = uuid4().hex[:8]
    before = _render_and_measure(ctx, f"audio-{p.stage}-before-{token}")
    applied = ApplyFairlightPresetResult.model_validate(
        ctx.transport(
            "project_settings", "apply_fairlight_preset", {"preset_name": binding.preset_name}
        )
    )
    if not applied.ok:
        raise LiveAdapterError(
            "apply-fairlight-preset-failed",
            f"apply of {binding.preset_name!r} refused: {applied.error}",
        )
    ctx.mark_timeline_mutated()
    after = _render_and_measure(ctx, f"audio-{p.stage}-after-{token}")
    value = _stage_value(p, before, after)
    if not p.minimum <= value <= p.maximum:
        # Deterministic post-mutation verdict: the preset already changed
        # the timeline, so a retry re-measures the mutated state (before ==
        # after) and would overwrite this attempt's real evidence with ~0.
        raise LiveAdapterError(
            "audio-stage-out-of-range",
            f"{p.stage} measured {p.metric} {value} {p.unit} outside "
            f"[{p.minimum}, {p.maximum}] — preset application blocked",
            retryable=False,
        )
    return {
        "stage": p.stage,
        "metric": p.metric,
        "unit": p.unit,
        "value": value,
        "preset": {
            "name": binding.preset_name,
            "applied": True,
            "binding": p.preset_ref,
            "kit_recipe_id": binding.kit_recipe_id,
        },
        "measured": {
            p.metric: value,
            "measured_from": "resolve_render",
            "before": before.model_dump(),
            "after": after.model_dump(),
        },
    }


def measure_audio_stage(
    ctx: LiveSessionContext, _action: str, params: Mapping[str, object]
) -> dict[str, object]:
    p = validate_params(AudioStageParams, params)
    if ctx.audio_measure is None:
        raise LiveAdapterError(
            "measurement-unavailable", "no rendered-media measurement port is wired"
        )
    # Bounded live control (rerun3): parameterless default runs an
    # unbounded matrix probe that never answers — precondition read only.
    boundary = RenderBoundaryReport.model_validate(
        ctx.transport("render", "export_render_boundary_report", {"include_matrix": False})
    )
    require_ok(boundary, "render-boundary-report")
    measured = _render_and_measure(ctx, f"audio-{p.stage}-{uuid4().hex[:8]}")
    if not p.minimum <= measured.integrated_loudness_lufs <= p.maximum:
        # Deterministic measured-policy verdict: the timeline is unchanged
        # between attempts, so every retry re-renders and re-measures the
        # same audio — bounded retry would only repeat identical evidence.
        raise LiveAdapterError(
            "audio-stage-out-of-range",
            f"{p.stage} measured {p.metric} {measured.integrated_loudness_lufs} "
            f"{p.unit} outside [{p.minimum}, {p.maximum}]",
            retryable=False,
        )
    return {
        "stage": p.stage,
        "metric": p.metric,
        "unit": p.unit,
        "value": measured.integrated_loudness_lufs,
        "measurements": {
            "measured_from": "resolve_render",
            "integrated_loudness_lufs": measured.integrated_loudness_lufs,
            "true_peak_dbtp": measured.true_peak_dbtp,
            "channels": measured.channels,
            "peak_mb": measured.peak_mb,
            "max_silence_ms": measured.max_silence_ms,
            "media_name": measured.media_name,
            "media_sha256": measured.media_sha256,
        },
    }


def _stage_value(p: AudioStageParams, before: MeasuredAudio, after: MeasuredAudio) -> float:
    if p.metric == "noise_reduction":
        # Loudness-RELATIVE floor improvement: how much farther the dialogue
        # rises above the noise floor, i.e. (after loudness-to-floor gap)
        # minus (before loudness-to-floor gap). A pure-gain preset moves
        # loudness and floor by the same amount and measures 0 dB (live
        # evidence: the tuned preset moved both +6 dB; the old absolute
        # floor delta mislabeled that as -6.198 dB). Derived only from the
        # two measured media rows.
        before_gap_db = before.integrated_loudness_lufs - before.floor_mb / 1000.0
        after_gap_db = after.integrated_loudness_lufs - after.floor_mb / 1000.0
        return after_gap_db - before_gap_db
    if p.metric == "dialogue_loudness":
        return after.integrated_loudness_lufs
    raise LiveAdapterUnsupportedError(
        "audio-stage-unmapped",
        f"no measured derivation for stage {p.stage!r} metric {p.metric!r}",
    )


__all__ = ["apply_dialogue_preset", "measure_audio_stage", "set_voice_isolation_state"]
