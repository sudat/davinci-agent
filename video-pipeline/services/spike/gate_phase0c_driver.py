"""Real evidence driver for the Phase-0C gate (pinned-ffmpeg, Resolve-free).

For every frozen fixture case: build the v1 plan from the manifest, open a
FRESH store/lineage per case (the frozen 0C contract caps one plan-mutating
apply per lineage), render the initial preview, replay-translate the frozen
instruction through the Todo-29 replay transport, re-validate with the
Todo-28 validator, and commit the operator-role decision through the Todo-30
writer. Clear cases additionally re-render preview-1 with the decision-bound
trace. Operator decisions are fixture-marked records inside the Spike; real
human approvals are NOT exercised (documented boundary).
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.foundation_io import atomic_write, canonical_model_bytes
from services.preview.models import AppliedDecision, PreviewTraceManifest
from services.preview.render import render_preview
from services.review_command.commit import commit_command
from services.review_command.store import (
    OperatorDecision0C,
    ReviewCommitError,
    initialize_store,
    load_head,
)
from services.review_command.translator import translate
from services.review_command.translator_policy import (
    DEFAULT_TOOLCHAIN_LOCK,
    load_frozen_contract,
)
from services.review_command.translator_schema import (
    TranslatorRequest,
    request_hash,
)
from services.review_command.translator_transport import ReplayTransport, StrictResponse
from services.spike.gate_phase0c_bindings import load_ir_file, media_bindings, preview_ir
from services.spike.gate_phase0c_case import (
    base_plan,
    load_case,
    replay_proposal,
    translator_request,
)
from services.spike.gate_phase0c_models import (
    EVENTS_LOG_NAME,
    PHASE_0C_CASES,
    PREVIEW0_DIR,
    PREVIEW1_DIR,
    run_dir,
    store_dir,
    translator_record_path,
)

if TYPE_CHECKING:
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.contracts.timeline_ir import TimelineIr0C
    from services.preview.tools import PinnedTools
    from services.review_command.models import ReviewCommandProposal0C

STORE_IR_NAMES: Final = {1: "ir-v1.json", 2: "ir-v2.json"}


class Gate0cDriverError(Exception):
    """The 0C evidence tree could not be completed within its bounds."""


@dataclass(frozen=True, slots=True)
class CaseDrive:
    fixture_id: str
    applied: bool
    deferred_reason: str | None
    request_hash: str
    decision_id: str


def _load_ir(store: Path, version: int) -> TimelineIr0C:
    return load_ir_file(store / STORE_IR_NAMES[version])


def translate_case(
    request: TranslatorRequest,
    proposal: ReviewCommandProposal0C,
    out_path: Path,
) -> None:
    transport = ReplayTransport(
        {request_hash(request): StrictResponse(canonical_model_bytes(proposal))}
    )
    result = translate(request, transport=transport)
    if result.status != "proposal" or result.proposal is None:
        raise Gate0cDriverError(
            f"replay translation did not produce a proposal (status={result.status})"
        )
    atomic_write(out_path, result.record.canonical_bytes())


def drive_case(
    fixture_id: str,
    evidence: Path,
    tools: PinnedTools,
    fixture_dir: Path,
) -> CaseDrive:
    manifest = load_case(fixture_id)
    plan: EditPlan0C = base_plan(manifest)
    run = run_dir(evidence, fixture_id)
    if run.is_dir():
        # Frozen 0C contract: one plan-mutating apply per lineage; the run dir
        # is gate-owned evidence rebuilt deterministically on every gate run.
        shutil.rmtree(run)
    store = store_dir(evidence, fixture_id)
    log = store / EVENTS_LOG_NAME
    initialize_store(plan, log, store)

    ir_v1 = _load_ir(store, 1)
    render_ir = preview_ir(ir_v1)
    trace_v1 = render_preview(
        plan if render_ir is ir_v1 else None,
        render_ir,
        media_bindings(render_ir, fixture_dir, run / "media", "v1"),
        run / PREVIEW0_DIR,
        tools=tools,
    )

    request = translator_request(manifest, plan)
    proposal = replay_proposal(manifest)
    translate_case(request, proposal, translator_record_path(evidence, fixture_id))
    decision = OperatorDecision0C(
        decision_id=f"decision-{fixture_id}",
        actor_intent="operator",
        note="gate fixture-marked operator-role record; human approvals are not exercised",
    )
    try:
        commit = commit_command(proposal, decision, log, store)
    except ReviewCommitError as error:
        raise Gate0cDriverError(f"{fixture_id}: commit refused: {error}") from error
    if commit.deferred:
        return CaseDrive(
            fixture_id=fixture_id,
            applied=False,
            deferred_reason=commit.reason,
            request_hash=request_hash(request),
            decision_id=decision.decision_id,
        )
    _render_after_apply(
        fixture_id, evidence, tools, fixture_dir, decision.decision_id, trace_v1
    )
    return CaseDrive(
        fixture_id=fixture_id,
        applied=True,
        deferred_reason=None,
        request_hash=request_hash(request),
        decision_id=decision.decision_id,
    )


def _render_after_apply(
    fixture_id: str,
    evidence: Path,
    tools: PinnedTools,
    fixture_dir: Path,
    decision_id: str,
    trace_v1: PreviewTraceManifest,
) -> None:
    run = run_dir(evidence, fixture_id)
    store = store_dir(evidence, fixture_id)
    head = load_head(store / EVENTS_LOG_NAME, store)
    ir_v2 = _load_ir(store, head.version)
    render_preview(
        head.plan,
        ir_v2,
        media_bindings(ir_v2, fixture_dir, run / "media", "v2"),
        run / PREVIEW1_DIR,
        tools=tools,
        decision=AppliedDecision(
            decision_id=decision_id,
            case_id=fixture_id,
            classification="clear",
            plan_version_after=f"v{head.version}",
            previous_trace=trace_v1,
        ),
    )


def drive_gate(
    evidence: Path,
    tools: PinnedTools,
    fixture_dir: Path,
    budget_seconds: float,
) -> tuple[CaseDrive, ...]:
    deadline = time.monotonic() + budget_seconds
    tools.verify_current()
    load_frozen_contract(DEFAULT_TOOLCHAIN_LOCK)
    driven: list[CaseDrive] = []
    for fixture_id in PHASE_0C_CASES:
        if time.monotonic() > deadline:
            raise Gate0cDriverError(f"gate budget exhausted before {fixture_id}")
        driven.append(drive_case(fixture_id, evidence, tools, fixture_dir))
        print(f"phase-0c: case {fixture_id} evidence complete")
    return tuple(driven)


__all__ = [
    "CaseDrive",
    "Gate0cDriverError",
    "drive_case",
    "drive_gate",
    "translate_case",
]
