"""The approved runtime chapter-title sidecar payload factory for tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

EPISODE_ID: Final = "ep-457dfac97989568e"

SIDECAR: Final[dict[str, object]] = {
    "schema_version": "v44-chapter-title-proposal-sidecar-v1",
    "proposal_only": True,
    "approval_status": "pending",
    "episode_id": EPISODE_ID,
    "base_plan_version": "v3",
    "base_plan_sha256": "bb025567cc1d10bb768aa595deec99af503631d6bbd3affbf6224ea487d4ebc3",
    "base_ir_sha256": "298a68764493019a409931cbedcd70a15bc70460ddc93f90c02ff3ee93bf6a12",
    "boundary": {
        "left_item_id": "s20",
        "right_item_id": "s21",
        "record_frame": 1632,
        "record_seconds": 54.4,
        "source_frame": 1845,
    },
    "evidence": {
        "sha256": "26c0e7aa7a0b1cb3db833ac4dfb770cd644e8dcad413170604d19e48ca1915cb",
        "cue_count": 79,
        "first_subtitle_id": "st21",
        "last_subtitle_id": "st99",
    },
    "model_pin": {
        "mode": "production_model",
        "transport": "codex-exec",
        "purpose": "editorial-director-v2",
        "model_id": "gpt-5.6-sol",
        "runtime_sha256": "0" * 64,
        "director_pin_sha256": "1" * 64,
    },
    "chapter_card_duration_frames": 45,
    "candidates": [
        {
            "candidate_id": "0" * 64,
            "presentation_intent": {
                "intent_id": "0" * 64,
                "kind": "chapter_card",
                "target_span": {"start_frame": 1632, "end_frame": 1677},
                "params": {"title": "候補その一", "duration_frames": 45},
                "rationale": "operator-selected chapter boundary; title remains pending",
            },
        },
        {
            "candidate_id": (
                "a08179562756c73040052a26091aa26da2118e5728dc3d22c8352048ce5e1914"
            ),
            "presentation_intent": {
                "intent_id": (
                    "a08179562756c73040052a26091aa26da2118e5728dc3d22c8352048ce5e1914"
                ),
                "kind": "chapter_card",
                "target_span": {"start_frame": 1632, "end_frame": 1677},
                "params": {"title": "どこにもないカメラバッグ", "duration_frames": 45},
                "rationale": "operator-selected chapter boundary; title remains pending",
            },
        },
        {
            "candidate_id": "2" * 64,
            "presentation_intent": {
                "intent_id": "2" * 64,
                "kind": "chapter_card",
                "target_span": {"start_frame": 1632, "end_frame": 1677},
                "params": {"title": "候補その三", "duration_frames": 45},
                "rationale": "operator-selected chapter boundary; title remains pending",
            },
        },
    ],
}


def write_sidecar(
    root: Path, *, mutate: str = "none", value: object = None, scaled: bool = False
) -> Path:
    """Write the runtime sidecar; ``scaled`` rebinds the boundary to frame 20.

    The 45-frame card duration is part of the frozen wire contract and stays.
    """

    payload = json.loads(json.dumps(SIDECAR))
    if scaled:
        payload["boundary"]["record_frame"] = 20
        payload["boundary"]["record_seconds"] = 20 / 30
        for candidate in payload["candidates"]:
            intent = candidate["presentation_intent"]
            intent["target_span"] = {"start_frame": 20, "end_frame": 65}
    if mutate == "candidate_id":
        payload["candidates"][1]["candidate_id"] = value
        payload["candidates"][1]["presentation_intent"]["intent_id"] = value
    elif mutate == "title":
        payload["candidates"][1]["presentation_intent"]["params"]["title"] = value
    elif mutate != "none":
        payload[mutate] = value
    path = root / "chapter-title-proposal.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


__all__ = ["EPISODE_ID", "SIDECAR", "write_sidecar"]
