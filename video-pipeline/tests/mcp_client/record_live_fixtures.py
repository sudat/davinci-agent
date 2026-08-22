"""Live fixture recorder for MCP contract fixtures (task 9/11).

Marked ``mcp_live``: the recording test runs ONLY when pytest selects
``-m mcp_live`` (task 11, operator environment with Resolve running).
Normal runs skip with an explicit reason.  When selected, the recorder
spawns the pinned server via the task-7 transport and is SELF-SUFFICIENT:
it creates its own throwaway project, imports one synthetic clip, and
places it on a timeline, so recording does not depend on whatever project
Resolve happens to have open.

Provenance of the two analysis fixtures (documented per-file in
``.recording-meta.json``): the compound server defers vision to the host
chat, so the recorded payloads are the ones the server ACCEPTED and
stored after the real local analysis pass (frames + shot_table come from
the live pending payload; the textual descriptions are host-authored for
synthetic calibration media).  Because those stored shapes are a different
payload class than the strict normalizer seeds, they are written to
``*.recorded.json`` — the synthetic ``media-analysis-standard.json`` and
``deep-shot-analysis.json`` seeds stay in place for the task-9 contract.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

from services.mcp_client.client import McpClient
from services.mcp_client.ops import McpOps
from services.mcp_client.ops_models import McpActionOutcome
from services.mcp_client.transport import StdioJsonRpcTransport, StdioTransportConfig
from services.qa.parity_mcp import generate_parity_media
from services.toolchain.mcp_pin import load_mcp_pin

FIXTURES_DIR: Final = Path(__file__).resolve().parent / "fixtures"
VIDEO_PIPELINE_ROOT: Final = Path(__file__).resolve().parents[2]
PIN_PATH: Final = VIDEO_PIPELINE_ROOT / "config" / "toolchains" / "davinci-resolve-mcp.pin.json"
RECORDING_META: Final = ".recording-meta.json"

PLANNED_FIXTURES: Final = frozenset(
    {
        "server-info.json",
        "tools-list.json",
        "resolve-version.json",
        # Recorded to *.recorded.json: the live server's stored full-V2 visual
        # and deepen-confirmed shots are a different payload class than the
        # strict normalizer seeds (which stay synthetic, see test_response_normalize).
        "media-analysis-standard.recorded.json",
        "deep-shot-analysis.recorded.json",
    }
)


def canonical_fixture_bytes(payload: object) -> bytes:
    """Canonical single-line JSON bytes (sort_keys, compact separators)."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8") + b"\n"


def _call_tool_text(client: McpClient, name: str, arguments: Mapping[str, object]) -> str:
    """One raw tools/call round-trip; returns the first text content block."""
    response = client.transport.request(
        "tools/call", {"name": name, "arguments": dict(arguments)}
    )
    result = response.get("result")
    if not isinstance(result, dict):
        raise TypeError(f"tool {name!r} returned a non-object result")
    content = result.get("content")
    if not isinstance(content, list) or not content:
        raise RuntimeError(f"tool {name!r} returned no content blocks")
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        raise RuntimeError(f"tool {name!r} returned a non-text content block")
    text = first.get("text")
    if not isinstance(text, str):
        raise TypeError(f"tool {name!r} returned a non-string text block")
    if result.get("isError"):
        raise RuntimeError(f"tool {name!r} reported an error: {text}")
    return text


def _prepare_clip(ops: McpOps, media: dict[str, Path], project_name: str) -> str:
    """Throwaway project + timeline with one placed clip; returns its clip id."""
    created = ops.prepare_project(project_name, 30)
    if not created.ok:
        raise RuntimeError(f"recorder project create failed: {created.error}")
    imported = ops.safe_import_media([str(media["parity-src-001"])])
    if not imported.ok or not imported.clips:
        raise RuntimeError(f"recorder media import failed: {imported.error}")
    clip = imported.clips[0]
    ready = ops.ensure_timeline(f"{project_name}-tl")
    if not ready.ok:
        raise RuntimeError(f"recorder timeline create failed: {ready.error}")
    appended = ops.append_to_timeline(
        [
            {
                "clip_id": clip.clip_id,
                "start_frame": 0,
                "end_frame": 120,
                "record_frame": 108000,
                "record_frame_mode": "absolute",
                "track_index": 1,
                "media_type": 1,
            }
        ]
    )
    if not appended.ok:
        raise RuntimeError(f"recorder timeline append failed: {appended.error}")
    return clip.clip_id


def _record_standard(ops: McpOps, clip_id: str) -> tuple[dict[str, object], dict[str, object]]:
    from tests.mcp_client.live_support import (  # noqa: PLC0415 (breaks an import cycle at collection)
        standard_visual_from_manifest,
    )

    executed = ops.analyze_clip(clip_id, dry_run=False, verbose=True)
    if not executed.ok or not executed.manifest:
        raise RuntimeError(f"analyze_clip executed pass failed: {executed.error}")
    visual = standard_visual_from_manifest(executed.manifest)
    if visual is None:
        raise RuntimeError("pending payload carried no shot_table to author visual from")
    committed = ops.commit_vision(visual, clip_id=clip_id)
    if not committed.ok or not committed.visual_json:
        raise RuntimeError(f"commit_vision failed: {committed.error}")
    stored: object = json.loads(Path(committed.visual_json).read_bytes())
    if not isinstance(stored, dict):
        raise TypeError("stored visual.json is not an object")
    meta = {
        "tool": "media_analysis",
        "flow": (
            "analyze_clip(dry_run=false) -> pending_host_vision_analysis (real frames + "
            "shot_table) -> host-authored V2 visual -> commit_vision -> server-normalized "
            "stored visual.json"
        ),
        "params": {"action": "analyze_clip", "clip_id": clip_id, "dry_run": False},
        "recorded_at": _utc_now(),
    }
    return stored, meta


def _record_deep(ops: McpOps, clip_id: str) -> tuple[dict[str, object], dict[str, object]]:
    from tests.mcp_client.live_support import (  # noqa: PLC0415 (breaks an import cycle at collection)
        deep_shots_from_payload,
    )

    estimate = ops.deepen(clip_id)
    if not estimate.ok:
        raise RuntimeError(f"deepen estimate failed: {estimate.error}")
    confirmed = ops.deepen(clip_id, confirm_token=estimate.confirm_token)
    if not confirmed.ok:
        raise RuntimeError(f"deepen confirmed payload failed: {confirmed.error}")
    payload_shots = [row for row in confirmed.shot_table if isinstance(row, dict)]
    if not payload_shots:
        raise RuntimeError("deepen confirmed payload carried no shot rows")
    shots = deep_shots_from_payload(payload_shots)
    committed = ops.commit_shot_vision(
        shots, clip_id=clip_id, vision_token=confirmed.vision_token
    )
    if not committed.ok:
        raise RuntimeError(f"commit_shot_vision failed: {committed.error}")
    fixture: dict[str, object] = {"shots": shots}
    meta = {
        "tool": "media_analysis",
        "flow": (
            "deepen -> confirm_token -> confirmed payload (real shot rows with uuids) -> "
            "host-authored deep groups -> commit_shot_vision accepted payload"
        ),
        "params": {"action": "deepen", "clip_id": clip_id},
        "recorded_at": _utc_now(),
    }
    return fixture, meta


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _record_all() -> None:
    """Spawn the pinned server, run the recording flow, overwrite the fixtures."""
    pin = load_mcp_pin(PIN_PATH)
    config = StdioTransportConfig.from_pin(pin)
    client = McpClient(StdioJsonRpcTransport(config))
    media_dir = Path(tempfile.mkdtemp(prefix="v43-record-media-"))
    project_name = f"v43-fixture-rec-{time.strftime('%H%M%S')}"
    try:
        client.connect()
        identity = client.get_server_info()
        server_identity = identity.model_dump(mode="json")
        recorded_at = _utc_now()
        ops = McpOps(client)
        clip_id = _prepare_clip(ops, generate_parity_media(media_dir), project_name)

        fixtures: dict[str, object] = {}
        meta: dict[str, object] = {}

        fixtures["server-info.json"] = server_identity
        meta["server-info.json"] = {
            "tool": "initialize",
            "params": {},
            "recorded_at": recorded_at,
            "server_identity": server_identity,
        }

        tools_payload: dict[str, object] = {
            "tools": [tool.model_dump(mode="json", by_alias=True) for tool in client.list_tools()]
        }
        fixtures["tools-list.json"] = tools_payload
        meta["tools-list.json"] = {
            "tool": "tools/list",
            "params": {},
            "recorded_at": recorded_at,
            "server_identity": server_identity,
        }

        version_text = _call_tool_text(client, "resolve_control", {"action": "get_version"})
        fixtures["resolve-version.json"] = json.loads(version_text)
        meta["resolve-version.json"] = {
            "tool": "resolve_control",
            "params": {"action": "get_version"},
            "recorded_at": recorded_at,
            "server_identity": server_identity,
        }

        standard, standard_meta = _record_standard(ops, clip_id)
        fixtures["media-analysis-standard.recorded.json"] = standard
        meta["media-analysis-standard.recorded.json"] = {
            **standard_meta,
            "server_identity": server_identity,
        }

        deep, deep_meta = _record_deep(ops, clip_id)
        fixtures["deep-shot-analysis.recorded.json"] = deep
        meta["deep-shot-analysis.recorded.json"] = {
            **deep_meta,
            "server_identity": server_identity,
        }

        for name, payload in fixtures.items():
            (FIXTURES_DIR / name).write_bytes(canonical_fixture_bytes(payload))
        (FIXTURES_DIR / RECORDING_META).write_bytes(canonical_fixture_bytes(meta))
    finally:
        with contextlib.suppress(Exception):
            ops_delete = McpOps(client)
            ops_delete._action(
                McpActionOutcome, "project_manager", "delete", {"name": project_name}
            )
        client.close()
        shutil.rmtree(media_dir, ignore_errors=True)


@pytest.mark.mcp_live
def test_record_live_fixtures(pytestconfig: pytest.Config) -> None:
    """Record live tool responses over the seed fixtures (task 11)."""
    markexpr = pytestconfig.getoption("markexpr") or ""
    if "mcp_live" not in markexpr:
        pytest.skip("live recording requires -m mcp_live")
    _record_all()
