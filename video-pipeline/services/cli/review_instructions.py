"""Canonical instruction renderings and canned replay proposals (Todo 45).

Each declared review command of a frozen fixture renders to ONE canonical
natural-language instruction; the replay translator accepts an instruction
only when it matches one of these renderings exactly. The canned proposal
payloads derive solely from the manifest spec and the current plan version
label — deterministic, no model call, no coercion of unknown instructions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.fixtures.manifest_phase1 import (
        Phase1TechnicalFixtureManifest,
        ReviewCommandSpec,
    )


class _InstructionError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def canonical_instruction(command: ReviewCommandSpec) -> str:
    target = command.target
    if command.operation == "remove_segment" and target.item_id is not None:
        return f"セグメント {target.item_id} を削除してください。"
    if command.operation == "adjust_source_span" and command.new_span is not None:
        return (
            f"セグメント {target.item_id} の尺を {command.new_span.start_frame}〜"
            f"{command.new_span.end_frame} フレームに調整してください。"
        )
    if command.new_text is None:
        raise _InstructionError(
            "declared_command_invalid", f"command {command.command_index} lacks new_text"
        )
    if target.kind == "subtitle_text_match" and target.text is not None:
        return f"「{target.text}」という字幕を「{command.new_text}」に修正してください。"
    if target.item_id is not None:
        return f"字幕 {target.item_id} を「{command.new_text}」に修正してください。"
    raise _InstructionError(
        "declared_command_invalid", f"command {command.command_index} has no usable target"
    )


def _selector_payload(command: ReviewCommandSpec) -> dict[str, object]:
    target = command.target
    if target.kind == "subtitle_text_match" and target.text is not None:
        return {"kind": "subtitle_text_match", "text": target.text}
    if target.item_id is not None:
        return {"kind": "item_id", "item_id": target.item_id}
    raise _InstructionError(
        "declared_command_invalid", f"command {command.command_index} has no usable target"
    )


def declared_proposal_payload(
    manifest: Phase1TechnicalFixtureManifest,
    command: ReviewCommandSpec,
    *,
    instruction: str,
    base_version: str,
) -> dict[str, object]:
    """The canned model output the replay transport returns for this command."""

    envelope: dict[str, object] = {
        "proposal_id": f"prop-{manifest.fixture_id}-{command.command_index}",
        "base_plan_version": base_version,
        "actor_intent": "model",
        "sequence": command.command_index,
        "confidence": {"num": 9, "den": 10},
        "ambiguity": {"status": "clear", "reasons": []},
        "evidence": [instruction],
    }
    if command.operation == "remove_segment" and command.target.item_id is not None:
        return envelope | {
            "command_kind": "remove_segment",
            "candidate_targets": [
                {"item_id": command.target.item_id, "evidence": instruction}
            ],
        }
    if command.operation == "adjust_source_span" and command.new_span is not None:
        return envelope | {
            "command_kind": "adjust_source_span",
            "target": _selector_payload(command),
            "new_span": {
                "start_frame": command.new_span.start_frame,
                "end_frame": command.new_span.end_frame,
            },
        }
    if command.new_text is None:
        raise _InstructionError(
            "declared_command_invalid", f"command {command.command_index} lacks new_text"
        )
    return envelope | {
        "command_kind": "correct_subtitle",
        "target": _selector_payload(command),
        "new_text": command.new_text,
        "language": "ja",
    }


__all__ = ["canonical_instruction", "declared_proposal_payload"]
