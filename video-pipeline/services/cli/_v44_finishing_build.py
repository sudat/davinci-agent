"""Shared load/execute pieces for the T13 finishing harness.

Deterministic, no CLI/argument parsing: ``services.cli.v44_finishing``
orchestrates these. Everything consumes the LATEST committed artifacts
(the cockpit review store wins over the frozen chain genesis store —
stale-state safe) and the operator's recorded kit selections
(``kit-selections.json`` from task 11). Plan assembly lives in
``_v44_finishing_plans``; the IR adapter in ``_v44_finishing_ir``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from services.cli.bundle import load_bundle
from services.cli.episode_runner_workspace import (
    COCKPIT_REVIEW_LOG_RELATIVE,
    COCKPIT_REVIEW_STORE_RELATIVE,
    RUN_DIR_NAME,
)
from services.cli.review_common import mezzanine_for, store_ir, store_plan
from services.creative_plan.quality_domains import (
    DomainExecutionV1,
    ExecutionFactsV1,
    QualityDomainName,
)
from services.mcp_client.client import McpToolCallError
from services.mcp_execution.plan_payloads import AudioMetricReadback
from services.production_kit.preview import (
    KitPreviewError,
    KitSelectionRecordV1,
    load_selection_record,
)
from services.review_command.store import load_head
from services.toolchain.mcp_fit import load_mcp_fit

if TYPE_CHECKING:
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.contracts.timeline_ir import TimelineIr0C
    from services.creative_plan.audio_finishing import AudioFinishingPlanV1
    from services.creative_plan.color_finishing import ColorFinishingPlanV1
    from services.creative_plan.subtitle_models import SubtitlePlanV1
    from services.mcp_execution.plan_models import McpExecutionPlanV1, McpExecutionStepV1
    from services.mcp_execution.runner import McpExecutionRunReportV1
    from services.production_kit.recipe_select import RecipeSelection

#: Which plan-step actions belong to which quality domain (episode0's task-43
#: adapter seam table, verbatim — documented in quality_domains.py).
DOMAIN_ACTIONS: Final[Mapping[str, frozenset[str]]] = {
    "subtitle": frozenset({"apply_subtitles"}),
    "audio_finishing": frozenset(
        {"apply_audio_stage", "apply_ducking", "apply_audio_op", "apply_voice_isolation"}
    ),
    "color_finishing": frozenset({"apply_color"}),
    "framing_motion": frozenset({"apply_transform"}),
    "graphics_presentation": frozenset({"place_title"}),
}
KIT_SELECTIONS_NAME: Final = "kit-selections.json"
MATRIX_PATH: Final = (
    Path(__file__).resolve().parents[2] / "capabilities" / "v4.4" / "mcp-fit.json"
)
PROPER_NOUNS_NAME: Final = "proper-nouns.json"


class FinishingError(Exception):
    """Typed refusal carrying the operator-actionable reason (exit 1)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class FinishingMalformedError(FinishingError):
    """Malformed input subclass (unreadable/corrupt files, bad args — exit 2)."""


@dataclass(frozen=True, slots=True)
class EpisodeContext:
    """The latest committed episode state the finishing run consumes."""

    episode_id: str
    head_version: int
    plan: EditPlan0C
    ir: TimelineIr0C
    mezzanine: Path


@dataclass(frozen=True, slots=True)
class FinishingPlans:
    """The assembled finishing plans + the resolved kit selections."""

    subtitle_plan: SubtitlePlanV1
    audio_plan: AudioFinishingPlanV1 | None
    color_plan: ColorFinishingPlanV1 | None
    recipe_selections: Mapping[str, RecipeSelection]
    kit_snapshot: tuple[dict[str, str | None], ...]
    notes: tuple[str, ...]


def resolve_review_store(episode_root: Path) -> tuple[Path, Path]:
    """Cockpit live store first; the frozen chain genesis second."""

    cockpit = (
        episode_root.joinpath(*COCKPIT_REVIEW_LOG_RELATIVE),
        episode_root.joinpath(*COCKPIT_REVIEW_STORE_RELATIVE),
    )
    if (cockpit[1] / "versions.json").is_file():
        return cockpit
    chain = (
        episode_root / RUN_DIR_NAME / "review-store" / "events.jsonl",
        episode_root / RUN_DIR_NAME / "review-store",
    )
    if (chain[1] / "versions.json").is_file():
        return chain
    raise FinishingError(
        "review-store-missing",
        f"no committed review store under {episode_root} (looked at "
        f"{'/'.join(COCKPIT_REVIEW_STORE_RELATIVE)} and {RUN_DIR_NAME}/review-store); "
        "the episode must reach PREVIEW_READY first",
    )


def load_episode_context(episode_root: Path) -> EpisodeContext:
    """Bundle (episode id + verified mezzanine) + the head plan/IR."""

    if not episode_root.is_dir():
        raise FinishingMalformedError(
            "episode-root-not-found", f"{episode_root} is not a directory"
        )
    bundle_file = episode_root / RUN_DIR_NAME / "review-bundle.json"
    if not bundle_file.is_file():
        raise FinishingError(
            "review-bundle-missing",
            f"no run/review-bundle.json under {episode_root}; "
            "finishing needs a PREVIEW_READY-or-later episode",
        )
    log_path, plan_dir = resolve_review_store(episode_root)
    try:
        bundle = load_bundle(bundle_file)
        head = load_head(log_path, plan_dir)
        plan = store_plan(plan_dir / f"plan-v{head.version}.json")
        ir = store_ir(plan_dir / f"ir-v{head.version}.json")
        mezzanine = mezzanine_for(bundle_file, bundle)
    except FinishingError:
        raise
    except Exception as error:
        raise FinishingMalformedError("committed-state-unreadable", str(error)) from error
    return EpisodeContext(
        episode_id=bundle.episode_id,
        head_version=head.version,
        plan=plan,
        ir=ir,
        mezzanine=mezzanine,
    )


def load_kit_selections(episode_root: Path) -> KitSelectionRecordV1:
    path = episode_root / KIT_SELECTIONS_NAME
    if not path.is_file():
        raise FinishingError(
            "kit-selections-missing",
            f"no {KIT_SELECTIONS_NAME} under {episode_root}; run the task-11 kit "
            "A/B previews and record operator selections first",
        )
    try:
        return load_selection_record(path)
    except KitPreviewError as error:
        if error.code in ("record-invalid", "record-unreadable"):
            raise FinishingMalformedError(
                f"kit-selections-{error.code}", error.detail
            ) from error
        raise FinishingError(f"kit-selections-{error.code}", error.detail) from error


def execution_facts(
    run_report: McpExecutionRunReportV1, episode_id: str
) -> ExecutionFactsV1:
    rows = []
    for domain, actions in DOMAIN_ACTIONS.items():
        steps = [s for s in run_report.steps if s.action in actions]
        if not steps:
            continue
        if any(s.status == "failed" for s in steps):
            outcome = "failed"
        elif any(s.rung == "manual_finalization" for s in steps):
            outcome = "manual_fallback"
        else:
            outcome = "executed"
        rows.append(
            DomainExecutionV1(
                domain=cast("QualityDomainName", domain), outcome=outcome
            )
        )
    return ExecutionFactsV1(episode_id=episode_id, domains=tuple(rows))


class FakePlanExecutor:
    """Replay executor: each step's expected readback becomes the actual.

    Same contract as the episode0 full-build fake (steps queue per action in
    plan order; audio-metric readbacks report the minimum measured value).
    No server, no I/O — the readback verifier still compares field by field.
    """

    def __init__(self, plan: McpExecutionPlanV1) -> None:
        self._queues: dict[str, list[McpExecutionStepV1]] = {}
        for step in plan.steps:
            self._queues.setdefault(step.action, []).append(step)

    def __call__(
        self,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object],  # noqa: ARG002 (protocol shape)
        *,
        timeout_seconds: float | None = None,  # noqa: ARG002 (protocol shape)
    ) -> object:
        queue = self._queues.get(action)
        if not queue:
            raise McpToolCallError(tool_name, f"unexpected action dispatched: {action}")
        step = queue.pop(0)
        actual = step.expected_readback.model_dump(mode="json")
        if isinstance(step.expected_readback, AudioMetricReadback):
            actual["value"] = step.expected_readback.minimum
        return actual


def mcp_lineage() -> dict[str, str]:
    """Provider/Resolve versions from the immutable v4.3 fit matrix."""

    document = load_mcp_fit(MATRIX_PATH)
    return {
        "provider_version": str(document["provider_version"]),
        "resolve_build": str(document["resolve_build"]),
    }


__all__ = [
    "DOMAIN_ACTIONS",
    "KIT_SELECTIONS_NAME",
    "MATRIX_PATH",
    "EpisodeContext",
    "FakePlanExecutor",
    "FinishingError",
    "FinishingMalformedError",
    "FinishingPlans",
    "execution_facts",
    "load_episode_context",
    "load_kit_selections",
    "mcp_lineage",
    "resolve_review_store",
]
