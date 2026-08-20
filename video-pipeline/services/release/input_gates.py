"""Typed release gates over staged candidate inputs (Todo 67).

Chat history and open Timelines are never sources of truth; staged Gate
reports must parse and actually have passed; no unsupported KPI claim may
ride along with a release candidate.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from services.foundation_io import sha256_file
from services.gates.models import GateResult
from services.release.errors import ReleaseGateError
from services.release.models import (
    GATE_DIRS,
    GATES_INPUT_DIR,
    H1_BINDING_NAME,
    H1_CHECKPOINT_NAME,
    H1_INPUT_DIR,
    INPUTS_DIR,
    H1TotalBinding,
)

_CHAT_ROW_KEYS = frozenset({"role", "content"})
_CHAT_EVENTS = frozenset({"message", "chat"})
_OPEN_TIMELINE_SUFFIXES = (".drp", ".drb")
_OPEN_TIMELINE_MARKERS = ("open-timeline", "chat-history", "chat_transcript")
_KPI_METRIC_PREFIX = "active_human_time"
_KPI_DIRECT_KEYS = ("active_human_time_median_ms", "active_human_time_p90_ms")


def check_gate_evidence(inputs_dir: Path) -> dict[str, str]:
    """Parse every staged Gate result; return ``{gate_dir: result_sha}``.

    Raises ``forged_report`` when a staged result fails its own recomputed
    pass state or naming, and ``failed_gate`` when one is missing or failed.
    """

    results: dict[str, str] = {}
    for gate_dir, expected_gate_id in sorted(GATE_DIRS.items()):
        path = inputs_dir / GATES_INPUT_DIR / gate_dir / "gate-result.json"
        if not path.is_file():
            raise ReleaseGateError("failed_gate", f"missing gate evidence: {gate_dir}")
        try:
            result = GateResult.model_validate_json(path.read_bytes())
        except ValidationError as error:
            raise ReleaseGateError(
                "forged_report", f"gate result for {gate_dir} fails recomputation: {error}"
            ) from error
        if result.gate_id != expected_gate_id:
            raise ReleaseGateError(
                "forged_report",
                f"gate dir {gate_dir} carries gate_id {result.gate_id}",
            )
        if not result.passed:
            raise ReleaseGateError("failed_gate", f"gate {gate_dir} did not pass")
        results[gate_dir] = sha256_file(path)
    return results


def _scan_jsonl_rows(path: Path) -> None:
    for line in path.read_bytes().splitlines():
        if not line.strip():
            continue
        try:
            row: object = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            keys = {key for key in row if isinstance(key, str)}
            if keys >= _CHAT_ROW_KEYS:
                raise ReleaseGateError(
                    "chat_or_timeline_dependency", f"chat-shaped ledger row staged: {path}"
                )
            event = row.get("event", row.get("type"))
            if isinstance(event, str) and event in _CHAT_EVENTS:
                raise ReleaseGateError(
                    "chat_or_timeline_dependency", f"chat event staged: {path}"
                )


def _scan_kpi_nodes(node: object, where: Path) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key in _KPI_DIRECT_KEYS:
                raise ReleaseGateError(
                    "misleading_kpi_claim", f"KPI metric staged in {where}: {key}"
                )
            if key == "metric" and isinstance(value, str) and value.startswith(_KPI_METRIC_PREFIX):
                raise ReleaseGateError(
                    "misleading_kpi_claim", f"KPI claim staged in {where}: {value}"
                )
            _scan_kpi_nodes(value, where)
    elif isinstance(node, list | tuple):
        for item in node:
            _scan_kpi_nodes(item, where)


def scan_inputs(inputs_dir: Path) -> None:
    """Reject chat/open-Timeline dependencies and unsupported KPI claims."""

    if not inputs_dir.is_dir():
        raise ReleaseGateError("missing_release_input", f"missing inputs directory: {inputs_dir}")
    stack = [inputs_dir]
    while stack:
        directory = stack.pop()
        for path in sorted(directory.iterdir()):
            if path.is_dir():
                stack.append(path)
                continue
            lowered = path.name.lower()
            posix = path.relative_to(inputs_dir).as_posix().lower()
            if lowered.endswith(_OPEN_TIMELINE_SUFFIXES) or any(
                marker in posix for marker in _OPEN_TIMELINE_MARKERS
            ):
                raise ReleaseGateError(
                    "chat_or_timeline_dependency", f"open-Timeline artifact staged: {path}"
                )
            if lowered.endswith(".jsonl"):
                _scan_jsonl_rows(path)
            if lowered.endswith(".json"):
                _scan_kpi_nodes(json.loads(path.read_bytes()), path)


def load_h1_binding(inputs_dir: Path) -> H1TotalBinding:
    """Load and fully verify the total H1 binding against its checkpoint."""

    binding_path = inputs_dir / H1_INPUT_DIR / H1_BINDING_NAME
    checkpoint_path = inputs_dir / H1_INPUT_DIR / H1_CHECKPOINT_NAME
    for path in (binding_path, checkpoint_path):
        if not path.is_file():
            raise ReleaseGateError("missing_h1_binding", f"missing H1 artifact: {path.name}")
    try:
        binding = H1TotalBinding.model_validate_json(binding_path.read_bytes())
        checkpoint: dict[str, object] = json.loads(checkpoint_path.read_bytes())
    except (ValidationError, ValueError) as error:
        raise ReleaseGateError("missing_h1_binding", f"H1 binding unparseable: {error}") from error
    if sha256_file(checkpoint_path) != binding.checkpoint_sha256:
        raise ReleaseGateError("stale_hash", "H1 binding does not hash-match its checkpoint")
    if checkpoint.get("fixture_only") is not False:
        raise ReleaseGateError(
            "missing_h1_binding", "H1 checkpoint is not a real operator checkpoint"
        )
    if checkpoint.get("purpose") != binding.decision:
        raise ReleaseGateError(
            "missing_h1_binding", "H1 checkpoint purpose is not EDITORIAL_APPROVED"
        )
    world = binding.bound_world
    for field, expected in (
        ("edit_source_world_sha256", world.edit_source_world_sha256),
        ("final_plan_sha256", world.final_plan_sha256),
        ("final_ir_sha256", world.final_ir_sha256),
        ("final_preview_sha256", world.final_preview_sha256),
    ):
        if checkpoint.get(field) != expected:
            raise ReleaseGateError("stale_hash", f"H1 binding drifts from checkpoint field {field}")
    if checkpoint.get("episode_id") != binding.episode_id:
        raise ReleaseGateError("stale_hash", "H1 binding episode drifts from checkpoint")
    return binding


def inputs_dir_of(candidate_root: Path) -> Path:
    return candidate_root / INPUTS_DIR


__all__ = [
    "check_gate_evidence",
    "inputs_dir_of",
    "load_h1_binding",
    "scan_inputs",
]
