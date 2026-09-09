"""r9d typed blocker ``preview-trace-duplicate-decision``: per-run rebuild ids.

The intent-only presentation path bumps no plan version (head stays at the
same version across rebuilds), so the pre-fix stamp
``decision-rebuild-{episode}-v{head.version}`` produced the SAME decision id
on a second rebuild and the trace manifest's uniqueness invariant refused the
append. The stamp now embeds the rebuild's own run id — derived and
deterministic, never a random uuid — so uniqueness is structural for BOTH the
intent-only and the consultation paths (single stamp seam in
``stage_preview``).
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from services.cli import episode_runner_rebuild
from services.cli.bundle import (
    ReviewTarget,
    assemble_real_bundle,
    save_bundle,
)
from services.cli.project import plan_sha256
from services.cli.review_common import store_ir, store_plan
from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
)
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.outputs.geometry import review_store_relatives
from services.preview.models import (
    AppliedDecision,
    PreviewFile,
    PreviewTraceManifest,
    RecordDecisionSpan,
    TimelineBinding,
    TraceDecision,
    TraceInput,
)
from services.preview.render import PREVIEW_NAME, TRACE_NAME
from services.preview.trace import decisions as trace_decisions
from services.preview.trace import rebuild_decision_id
from services.review_command.store import initialize_store, load_head
from tests.episode_cockpit.test_rebuild_executor import _seed_plan

if TYPE_CHECKING:
    from collections.abc import Callable

    from services.contracts.edit_plan_0c import EditPlan0C
    from services.contracts.timeline_ir import TimelineIr0C
    from services.review_command.store import HeadState

EPISODE_ID = "ep-r9d"
RATE = RationalFrameRate(num=30, den=1)

_STRATEGY_NOTES = {
    "subtitle_rung": "omitted-no-subtitle-items",
    "overlay_strategy": "lavfi-color-corner-marker-overlay",
    "audio_strategy": "item-linked-concat-single-track-no-bgm-binding",
    "determinism_policy": "semantic-equivalence-h264-videotoolbox",
}

_FFPROBE = {
    "stream_count": 2,
    "video_codec": "h264",
    "width": 640,
    "height": 360,
    "r_frame_rate": "30/1",
    "avg_frame_rate": "30/1",
    "nb_read_frames": 30,
    "video_duration_ms": 1000,
    "container_duration_ms": 1000,
    "audio_codec": "aac",
    "audio_sample_rate": 48000,
    "audio_channels": 2,
    "subtitle_codec": None,
}


def _trace_with_decisions(decision_ids: tuple[str, ...]) -> PreviewTraceManifest:
    """A valid trace manifest whose decision chain is exactly ``decision_ids``."""

    record = RecordFrameSpan(start_frame=0, end_frame=30)
    return PreviewTraceManifest(
        schema_version="preview-trace-v1",
        preview=PreviewFile(
            path="/fake-trace-only/never-rendered.mp4",
            sha256="0" * 64,
            size=1,
            decoded_video_sha256="0" * 64,
        ),
        timeline_binding=TimelineBinding(
            plan_version="v1",
            ir_sha256="0" * 64,
            total_record_frames=30,
            timeline_rate=RATE,
        ),
        inputs=(
            TraceInput(
                item_id="cut-001",
                kind="video",
                media_path="/fake-trace-only/never-touched.mov",
                sha256="0" * 64,
                source_span=SourceFrameSpan(start_frame=0, end_frame=30, rate=RATE),
                record_span=record,
            ),
        ),
        decisions=tuple(
            TraceDecision(
                decision_id=decision_id,
                case_id="initial-plan" if decision_id.startswith("initial-") else EPISODE_ID,
                classification="initial" if decision_id.startswith("initial-") else "clear",
                applied=True,
                plan_version_after="v1",
            )
            for decision_id in decision_ids
        ),
        record_to_decision=(
            RecordDecisionSpan(span=record, decision_id=decision_ids[-1]),
        ),
        strategy_notes=_STRATEGY_NOTES,
        ffprobe_summary=_FFPROBE,
    )


def _preview_ready_episode(
    tmp_path: Path,
) -> tuple[Path, HeadState, EditPlan0C, TimelineIr0C]:
    """PREVIEW_READY episode at head v1 whose trace already holds one rebuild."""

    episode_dir = tmp_path / EPISODE_ID
    run_dir = episode_dir / "run"
    log_rel, store_rel = review_store_relatives("landscape")
    log_path = episode_dir.joinpath(*log_rel)
    store_dir = episode_dir.joinpath(*store_rel)
    seed = _seed_plan()
    initialize_store(seed, log_path, store_dir)
    head = load_head(log_path, store_dir)

    mezzanine = run_dir / "media" / "edit-source.mov"
    mezzanine.parent.mkdir(parents=True, exist_ok=True)
    mezzanine.write_bytes(b"fake-mezzanine")
    preview_dir = run_dir / "preview-v1"
    preview_dir.mkdir(parents=True, exist_ok=True)
    r9c_stamp = rebuild_decision_id(EPISODE_ID, 1, "run0000r9crun")
    atomic_write(
        preview_dir / TRACE_NAME,
        canonical_model_bytes(_trace_with_decisions(("initial-plan-v1", r9c_stamp))),
    )
    mezz_sha = sha256_file(mezzanine)
    save_bundle(
        assemble_real_bundle(
            episode_id=EPISODE_ID,
            eligibility_status="supported",
            mezzanine_sha256=mezz_sha,
            edit_source_world_sha256=mezz_sha,
            episode_manifest_sha256=mezz_sha,
            policy_sha256=mezz_sha,
            target=ReviewTarget(
                plan_version="v1",
                plan_sha256=plan_sha256(seed),
                ir_sha256=sha256_file(store_dir / "ir-v1.json"),
                preview_dir="preview-v1",
                preview_sha256=mezz_sha,
                trace_sha256=sha256_file(preview_dir / TRACE_NAME),
            ),
        ),
        run_dir / "review-bundle.json",
    )
    return episode_dir, head, store_plan(store_dir / "plan-v1.json"), store_ir(
        store_dir / "ir-v1.json"
    )

def _trace_appending_render(
    captured: list[AppliedDecision],
) -> Callable[..., None]:
    """Fake render that records the decision and appends it like build_trace."""

    def fake_render(  # noqa: PLR0913 (mirrors the real render_review_preview seam)
        _plan: EditPlan0C,
        _ir: TimelineIr0C,
        _mezzanine: Path,
        preview_dir: Path,
        *,
        tools: object,
        decision: AppliedDecision,
        timeout_seconds: float | None = None,
        presentation: object = None,
        presentation_trace: object = None,
    ) -> None:
        captured.append(decision)
        preview_dir.mkdir(parents=True, exist_ok=True)
        (preview_dir / PREVIEW_NAME).write_bytes(b"rebuild-preview")
        previous = PreviewTraceManifest.model_validate_json(
            (preview_dir / TRACE_NAME).read_bytes()
        )
        appended = trace_decisions(previous.timeline_binding.plan_version, decision)
        updated = PreviewTraceManifest.model_validate(
            previous.model_dump(mode="json")
            | {"decisions": [entry.model_dump(mode="json") for entry in appended]}
        )
        atomic_write(preview_dir / TRACE_NAME, canonical_model_bytes(updated))

    return fake_render


def test_two_intent_only_rebuilds_at_same_head_get_distinct_accepted_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) two intent-only rebuilds at head v1: distinct ids, both accepted."""

    episode_dir, head, plan, ir = _preview_ready_episode(tmp_path)
    assert head.version == 1
    r9c_stamp = rebuild_decision_id(EPISODE_ID, 1, "run0000r9crun")
    captured: list[AppliedDecision] = []
    monkeypatch.setattr(
        episode_runner_rebuild, "render_review_preview", _trace_appending_render(captured)
    )
    monkeypatch.setattr(episode_runner_rebuild, "load_tools", object)
    log = io.BytesIO()

    episode_runner_rebuild.stage_preview(
        episode_dir, head, plan, ir, log, run_id="runaa11bb22cc"
    )
    episode_runner_rebuild.stage_preview(
        episode_dir, head, plan, ir, log, run_id="rundd33ee44ff"
    )

    assert head.version == 1  # intent-only: no version bump between rebuilds
    first_id, second_id = (entry.decision_id for entry in captured)
    assert first_id == f"decision-rebuild-{EPISODE_ID}-v1-runaa11bb22cc"
    assert second_id == f"decision-rebuild-{EPISODE_ID}-v1-rundd33ee44ff"
    assert first_id != second_id
    final_trace = PreviewTraceManifest.model_validate_json(
        (episode_dir / "run" / "preview-v1" / TRACE_NAME).read_bytes()
    )
    assert [entry.decision_id for entry in final_trace.decisions] == [
        "initial-plan-v1",
        r9c_stamp,
        first_id,
        second_id,
    ]


def test_consultation_path_rebuild_ids_stay_unique_and_format_consistent() -> None:
    """(b) consultation re-adoption stamps the SAME run-suffixed format."""

    v2_first = rebuild_decision_id(EPISODE_ID, 2, "runaaa1112223")
    v2_again = rebuild_decision_id(EPISODE_ID, 2, "runbbb3334444")
    v3_later = rebuild_decision_id(EPISODE_ID, 3, "runccc5556667")
    pattern = rf"^decision-rebuild-{EPISODE_ID}-v[1-9][0-9]*-[0-9a-z]+$"
    assert re.fullmatch(pattern, v2_first)
    assert re.fullmatch(pattern, v2_again)
    assert re.fullmatch(pattern, v3_later)
    assert len({v2_first, v2_again, v3_later}) == 3
    assert v3_later.startswith(f"decision-rebuild-{EPISODE_ID}-v3-")


def test_rebuild_decision_id_is_deterministic_from_recorded_facts() -> None:
    """(c) same episode + version + run → byte-identical id, always."""

    expected = "decision-rebuild-ep-2042e23f1ae22b66-v4-ee0cfb0c4cd8"
    assert rebuild_decision_id("ep-2042e23f1ae22b66", 4, "ee0cfb0c4cd8") == expected
    assert rebuild_decision_id(
        "ep-2042e23f1ae22b66", 4, "ee0cfb0c4cd8"
    ) == rebuild_decision_id("ep-2042e23f1ae22b66", 4, "ee0cfb0c4cd8")


def test_manifest_uniqueness_invariant_still_refuses_duplicate_ids() -> None:
    """The pre-fix r9d collision shape (same id appended twice) stays refused."""

    stamp = rebuild_decision_id(EPISODE_ID, 1, "run0000r9crun")
    with pytest.raises(ValidationError, match="duplicate_decision"):
        _trace_with_decisions(("initial-plan-v1", stamp, stamp))
