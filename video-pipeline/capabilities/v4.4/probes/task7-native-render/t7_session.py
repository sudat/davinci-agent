"""Task 7 probe session: constants, plan compilation, instrumented executor.

The disposable project's steps are compiled with the production emitters
(``step_from`` builders + ``plan_steps.render_native_steps``) into an
``McpExecutionPlanV1``. The executor is the real ``LiveMcpAdapter`` wrapped
READ-ONLY: each runner-dispatched ``render_native`` call's actual return
value is captured for evidence — no second writer, no runner bypass.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Protocol, TypedDict, cast

t6_path = Path(__file__).resolve().parents[1] / "task6-color-drx" / "t6_session.py"
spec = importlib.util.spec_from_file_location("t6_session", t6_path)
mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(mod)  # type: ignore[union-attr]

VIDEO_PIPELINE = mod.VIDEO_PIPELINE
sys.path.insert(0, str(VIDEO_PIPELINE))

from services.contracts.primitives import (  # noqa: E402
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.job_runner.stage_runner_models import SequenceClock  # noqa: E402
from services.job_runner.state_store import StateStore  # noqa: E402, TC001
from services.mcp_client.call_models import McpCallRecorder  # noqa: E402
from services.mcp_execution.live_adapter import LiveMcpAdapter  # noqa: E402
from services.mcp_execution.plan_models import (  # noqa: E402
    McpExecutionPlanV1,
    compute_plan_id,
)
from services.mcp_execution.plan_payloads import (  # noqa: E402
    ImportMediaParams,
    ImportReadback,
    PlaceClipParams,
    PlacementReadback,
    PrepareProjectParams,
    ProjectReadback,
)
from services.mcp_execution.plan_steps import render_native_steps  # noqa: E402
from services.mcp_execution.runner import McpExecutionRunnerV2  # noqa: E402
from services.mcp_execution.step_builders import (  # noqa: E402
    CapabilityView,
    step_from,
)

if TYPE_CHECKING:
    from services.mcp_client.client import McpClient

PROBE_DIR = Path(__file__).resolve().parent
LEDGER_DIR = PROBE_DIR / "ledger"
RENDER_DIR = PROBE_DIR / "render"
SCRATCH = mod.SCRATCH
SUMMARY = PROBE_DIR / "summary.json"
EPISODE_ID = "t7probe"
CUSTOM_NAME = f"finishing-native-{EPISODE_ID}"
SOURCE_ID = "src-t7"
PROJECT_NAME = f"probe-t7render-{uuid.uuid4().hex[:6]}"
JOB_ID = f"job-t7probe-{uuid.uuid4().hex[:6]}"
HOLDER = "t7-probe"
STATE_DB = VIDEO_PIPELINE / "jobs" / "t7probe-state.sqlite3"
BACKENDS = VIDEO_PIPELINE / "config" / "backends.json"


class _CaptureSeam(Protocol):
    """The two private client members the vendor-call capture touches."""

    _call_tool: Callable[..., object]

    def _call_action_json(
        self, tool: str, action: str, params: Mapping[str, object]
    ) -> object: ...


class ProbeState(TypedDict):
    """The disposable session bag returned by the shared t6 helper."""

    client: McpClient
    provider_version: str
    resolve_version: str


def open_session() -> ProbeState:
    """Connect the pinned MCP/Resolve via the shared t6 session helper."""

    return cast("ProbeState", mod.open_session())


def close_session(state: ProbeState) -> None:
    mod.close_session(state)  # type: ignore[arg-type]


def build_executor(state: ProbeState, clip: Path) -> LiveMcpAdapter:
    """The real adapter over a client whose tools/call seam logs vendor calls.

    The capture seam shadows the instance attribute via ``setattr`` — a
    read-only observation of every vendor call, not a second writer.
    """

    client: McpClient = state["client"]
    original_call_tool = cast("_CaptureSeam", client)._call_tool  # noqa: SLF001
    vendor_log = LEDGER_DIR / "vendor-calls.jsonl"

    def capturing_call_tool(
        name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        result = original_call_tool(
            name, arguments, timeout_seconds=timeout_seconds
        )
        action = arguments.get("action", "-") if isinstance(arguments, dict) else "-"
        with vendor_log.open("a") as stream:
            stream.write(json.dumps({"tool": name, "action": str(action)}) + "\n")
        return result

    # Shadow the bound method on the instance (read-only vendor capture).
    seam = cast("_CaptureSeam", client)
    seam._call_tool = capturing_call_tool  # noqa: SLF001 (observation seam)

    def transport(
        tool_name: str, action: str, normalized_params: Mapping[str, object]
    ) -> object:
        return seam._call_action_json(tool_name, action, normalized_params)  # noqa: SLF001

    return LiveMcpAdapter(
        transport,
        media_paths={SOURCE_ID: str(clip)},
        render_dir=str(RENDER_DIR),
    )


def build_plan() -> McpExecutionPlanV1:
    span = SourceFrameSpan(start_frame=0, end_frame=30, rate=RationalFrameRate(num=30, den=1))
    prepare = step_from(
        "stp-prepare",
        PrepareProjectParams(
            action="prepare_project", timeline_name=PROJECT_NAME, fps_num=30, fps_den=1
        ),
        ProjectReadback(kind="project", project_name=PROJECT_NAME, timeline_frame_rate="30"),
        "prepare_project",
        "mcp_verified_workflow",
        None,
        0,
    )
    imp = step_from(
        "stp-import",
        ImportMediaParams(action="import_media", source_id=SOURCE_ID),
        ImportReadback(kind="import", source_id=SOURCE_ID),
        "safe_import_media",
        "mcp_verified_workflow",
        None,
        1,
    )
    place = step_from(
        "stp-place",
        PlaceClipParams(
            action="place_clip",
            item_id="t7-1",
            source=SourceRef(source_id=SOURCE_ID, span=span),
            record_span=RecordFrameSpan(start_frame=0, end_frame=30),
            track_role="primary",
        ),
        PlacementReadback(
            kind="placement",
            item_id="t7-1",
            source_span=span,
            record_span=RecordFrameSpan(start_frame=0, end_frame=30),
        ),
        "append_to_timeline",
        "mcp_verified_workflow",
        None,
        2,
        media=(SOURCE_ID,),
    )
    ir_stub = SimpleNamespace(episode_id=EPISODE_ID)
    steps = (prepare, imp, place, *render_native_steps(ir_stub, CapabilityView({}, {}), 30))  # type: ignore[arg-type]
    return McpExecutionPlanV1(
        schema_version="mcp-execution-plan-v1",
        plan_id=compute_plan_id(EPISODE_ID, steps),  # type: ignore[arg-type]
        episode_id=EPISODE_ID,
        steps=steps,  # type: ignore[arg-type]
    )


def build_runner(store: StateStore) -> McpExecutionRunnerV2:
    return McpExecutionRunnerV2(
        store=store,
        job_id=JOB_ID,
        stage_name="stage-finish",
        holder_token=HOLDER,
        ledger_dir=LEDGER_DIR,
        clock=SequenceClock(1000),
        backends_path=BACKENDS,
    )


def build_recorder(lineage: dict[str, str]) -> McpCallRecorder:
    return McpCallRecorder(
        provider_version=lineage["provider_version"],
        resolve_version=lineage["resolve_version"],
        server_mode="compound",
        clock=lambda: 0,
    )


__all__ = [
    "BACKENDS",
    "CUSTOM_NAME",
    "HOLDER",
    "JOB_ID",
    "LEDGER_DIR",
    "RENDER_DIR",
    "STATE_DB",
    "SUMMARY",
    "VIDEO_PIPELINE",
    "build_executor",
    "build_plan",
    "build_recorder",
    "build_runner",
    "close_session",
    "open_session",
]
