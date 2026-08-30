"""Live color-domain handler: explicit-target DRX application (Task 6).

Hash-pinned kit DRX (staged into the system temp dir so the vendor's
temp-path guard holds) applied to EXPLICIT targets only
(``color_targets`` resolves item ids to vendor track/item coordinates —
never a selection or the implicit V1/item0). Per target: version/graph
snapshot, recoverable pre-grade version, the vendor dry-run →
confirmation token → apply flow untouched, then graph re-read plus
matched-frame evidence (``color_evidence``) from fresh before/after
renders. Reruns detect the DRX-identity grade version and never re-apply.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import uuid4

from services.foundation_io import sha256_file
from services.mcp_client.ops_models import DrxApplyResult, NodeGraphResult
from services.mcp_execution.live_handlers.audio_render import render_fresh_audio
from services.mcp_execution.live_handlers.color_evidence import (
    verify_target_frames,
    verify_untargeted_frames,
)
from services.mcp_execution.live_handlers.color_targets import (
    ResolvedTarget,
    add_version,
    compensate_orientation,
    coords,
    grade_versions,
    graph_signature,
    node_graph,
    representative_frame,
    resolve_targets,
    structure_snapshot,
)
from services.mcp_execution.live_handlers.common import (
    LiveAdapterError,
    LiveAdapterUnsupportedError,
    LiveSessionContext,
    validate_params,
)
from services.mcp_execution.plan_payloads import ColorParams

#: MEASURED rationale for the +90 DRX orientation compensation lives at
#: ``color_targets.DRX_ORIENTATION_COMPENSATION_ANGLE`` — the seam owning
#: resolved target coordinates also owns the compensation helper.

_PIPELINE_ROOT = Path(__file__).resolve().parents[3]
_CONFIRMATION_REQUIRED: Final = "CONFIRMATION_REQUIRED"


@dataclass(frozen=True)
class DrxBinding:
    """One kit selection → concrete hash-pinned DRX identity."""

    drx_ref: str
    kit_recipe_id: str
    drx_path: Path
    sha256: str


_DRX_BINDINGS: Final[Mapping[str, DrxBinding]] = {
    "drx-technical-normalize-v1": DrxBinding(
        drx_ref="drx-technical-normalize-v1",
        kit_recipe_id="color/technical-normalize",
        drx_path=_PIPELINE_ROOT / "config" / "production-kit" / "drx" / "technical-normalize-v1.drx",  # noqa: E501
        sha256="509e70f94fe33667a5f0ee92dfb36cf6106bf8f086e962b41ca93cd2c1c30269",
    ),
}


def _binding(p: ColorParams) -> DrxBinding:
    if p.drx_ref is None:
        raise LiveAdapterUnsupportedError(
            "drx-ref-missing",
            f"section {p.section!r} carries no DRX binding ref; only the "
            "technical_correction.exposure path is wired live",
        )
    binding = _DRX_BINDINGS.get(str(p.drx_ref))
    if binding is None:
        raise LiveAdapterUnsupportedError(
            "drx-binding-unknown", f"drx_ref {p.drx_ref!r} has no hash-pinned DRX binding"
        )
    return binding


def _stage_drx(binding: DrxBinding) -> Path:
    if not binding.drx_path.is_file():
        raise LiveAdapterUnsupportedError(
            "drx-missing", f"pinned DRX {binding.drx_path.name} is absent from the kit"
        )
    digest = sha256_file(binding.drx_path)
    if digest != binding.sha256:
        raise LiveAdapterError(
            "drx-hash-mismatch",
            f"pinned DRX {binding.drx_path.name} hashes to {digest}, binding pins {binding.sha256}",
        )
    staged_dir = Path(tempfile.mkdtemp(prefix="t6-drx-"))
    staged = staged_dir / binding.drx_path.name
    shutil.copyfile(binding.drx_path, staged)
    if sha256_file(staged) != binding.sha256:
        raise LiveAdapterError("drx-hash-mismatch", f"staged copy of {staged.name} failed hash pin")
    return staged





def _apply_call(
    ctx: LiveSessionContext, target: ResolvedTarget, staged: Path, token: str | None
) -> DrxApplyResult:
    params: dict[str, object] = {**coords(target), "path": str(staged), "dry_run": False, "grade_mode": 0}  # noqa: E501
    if token is not None:
        params["confirm_token"] = token
    return DrxApplyResult.model_validate(
        ctx.transport("timeline_item_color", "safe_apply_drx", params)
    )


def _confirmed_apply(
    ctx: LiveSessionContext, target: ResolvedTarget, staged: Path
) -> dict[str, object]:
    dry = DrxApplyResult.model_validate(
        ctx.transport(
            "timeline_item_color",
            "safe_apply_drx",
            {**coords(target), "path": str(staged), "dry_run": True, "grade_mode": 0},
        )
    )
    if not dry.ok or dry.would_apply is not True:
        raise LiveAdapterError("drx-dry-run-failed", f"dry-run refused: {dry.error}")
    confirm = _apply_call(ctx, target, staged, None)
    if confirm.ok:
        ctx.mark_timeline_mutated()
        return {"required": False}
    if (
        confirm.confirm_token is None
        or confirm.error is None
        or confirm.error.code != _CONFIRMATION_REQUIRED
    ):
        raise LiveAdapterError(
            "drx-confirmation-missing", f"no confirmation token issued: {confirm.error}"
        )
    applied = _apply_call(ctx, target, staged, confirm.confirm_token)
    if not applied.ok:
        raise LiveAdapterError("drx-apply-failed", f"confirmed apply refused: {applied.error}")
    ctx.mark_timeline_mutated()
    return {"required": True, "token_issued": True}


def _entry(
    target: ResolvedTarget,
    identity: str,
    confirmation: dict[str, object],
    graphs: tuple[NodeGraphResult, NodeGraphResult],
    *,
    already: bool,
) -> dict[str, object]:
    return {
        "item_id": str(target.payload.item_id),
        "timeline_item_id": target.timeline_item_id,
        "grade_version": identity,
        "already_applied": already,
        "confirmation": confirmation,
        "graph": {
            "before_num_nodes": graphs[0].num_nodes,
            "after_num_nodes": graphs[1].num_nodes,
        },
        "target_frame": representative_frame(target),
    }


def _apply_pending(
    ctx: LiveSessionContext, binding: DrxBinding, staged: Path, target: ResolvedTarget
) -> tuple[dict[str, object], str | None]:
    """Apply the DRX to one target without recording the identity version.

    The identity marker is returned as the second element and must only be
    recorded after all graph and frame evidence for the entire run passes.
    """

    identity = f"drx-{binding.drx_ref}-{binding.sha256[:8]}"
    pre_name = f"pre-drx-{binding.sha256[:8]}"
    before_versions = grade_versions(ctx, target)
    before_graph = node_graph(ctx, target)
    names = {str(name) for name in before_versions.local}
    if identity in names:
        already_entry = _entry(
            target, identity, {"required": False}, (before_graph, before_graph), already=True
        )
        return already_entry, None
    if pre_name not in names:
        add_version(ctx, target, pre_name)
    confirmation = _confirmed_apply(ctx, target, staged)
    after_graph = node_graph(ctx, target)
    if graph_signature(after_graph) == graph_signature(before_graph):
        raise LiveAdapterError(
            "graph-unchanged", f"target {target.payload.item_id} graph identical after apply"
        )
    pending_entry = _entry(
        target, identity, confirmation, (before_graph, after_graph), already=False
    )
    return pending_entry, identity


def apply_drx_grade(
    ctx: LiveSessionContext, _action: str, params: Mapping[str, object]
) -> dict[str, object]:
    p = validate_params(ColorParams, params)
    binding = _binding(p)
    if not p.targets:
        raise LiveAdapterError("color-targets-missing", "explicit target item ids are required")
    if ctx.frame_diff is None:
        raise LiveAdapterError(
            "frame-evidence-unavailable", "no rendered-frame comparison port is wired"
        )
    staged = _stage_drx(binding)
    # Probe renders are rebuildable: both are unlinked in the finally AFTER
    # every frame comparison captured its evidence (success and typed-failure
    # paths alike — the user-reported leak left ~21 GiB of probe videos).
    before_render: Path | None = None
    after_render: Path | None = None
    try:
        snap = structure_snapshot(ctx)
        targets = resolve_targets(ctx, snap, p.targets)
        run = uuid4().hex[:8]
        before_render = render_fresh_audio(ctx, f"color-{p.section}-before-{run}")
        pending: list[tuple[ResolvedTarget, str]] = []
        entries: list[dict[str, object]] = []
        for target in targets:
            entry, identity = _apply_pending(ctx, binding, staged, target)
            entries.append(entry)
            if identity is not None:
                pending.append((target, identity))
        after_render = render_fresh_audio(ctx, f"color-{p.section}-after-{run}")
        target_ids = {target.timeline_item_id for target in targets}
        target_spans = tuple(
            (target.payload.record_span.start_frame, target.payload.record_span.end_frame)
            for target in targets
        )
        untargeted = verify_untargeted_frames(
            ctx,
            snap,
            target_ids,
            target_spans=target_spans,
            before=before_render,
            after=after_render,
        )
        verify_target_frames(ctx, entries, before_render, after_render)
        for target, entry in zip(targets, entries, strict=True):
            entry["orientation_compensation"] = compensate_orientation(ctx, target)
        for target, identity in pending:
            add_version(ctx, target, identity)
        return {
            "target": p.section,
            "preset_ref": str(binding.drx_ref),
            "drx_sha256": binding.sha256,
            "kit_recipe_id": binding.kit_recipe_id,
            "applied_targets": entries,
            "untargeted_frames": untargeted,
        }
    finally:
        for probe in (before_render, after_render):
            if probe is not None:
                probe.unlink(missing_ok=True)
        shutil.rmtree(staged.parent, ignore_errors=True)


__all__ = ["apply_drx_grade"]
