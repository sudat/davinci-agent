"""Structured 本人確認 record (工程6 prep, additive).

One append-only JSONL file per episode (``self-check.jsonl``) holds
``ObservationSelfCheckV1`` lines. Operator-supplied only — the server
never defaults answers (None = 未回答 honestly persisted) and never
auto-creates the file; absent file reads as None everywhere (status
view, observer, GET). Writes append one line; reads scan the tail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from services.episode_cockpit.workspace_context import WorkspaceContext, validated_episode_id
from services.metrics.v44_gate_state import ObservationSelfCheckV1

if TYPE_CHECKING:
    from services.episode_cockpit.models import SelfCheckRequest

SELF_CHECK_NAME: Final = "self-check.jsonl"


def _read_self_checks(episode_dir: Path) -> list[ObservationSelfCheckV1]:
    """All valid self-check lines (corrupt lines are skipped, never crash)."""

    try:
        raw = (episode_dir / SELF_CHECK_NAME).read_bytes().splitlines()
    except OSError:
        return []
    records: list[ObservationSelfCheckV1] = []
    for line in raw:
        if not line.strip():
            continue
        try:
            records.append(ObservationSelfCheckV1.model_validate_json(line))
        except ValidationError:
            continue
    return records


def latest_self_check(episode_dir: Path) -> ObservationSelfCheckV1 | None:
    """Latest self-check line, or None when never answered (honest absent)."""

    records = _read_self_checks(episode_dir)
    return records[-1] if records else None


class SelfCheckOps(WorkspaceContext):
    """Append/read the operator-supplied 本人確認 record."""

    def record_self_check(
        self, episode_id: str, request: SelfCheckRequest
    ) -> dict[str, object]:
        validated = validated_episode_id(episode_id)
        self._require_snapshot(validated)
        record = ObservationSelfCheckV1(
            episode_id=validated,  # type: ignore[arg-type]
            answered_at=datetime.now(UTC).isoformat(),
            q_instruction_transmitted=request.q_instruction_transmitted,
            q_better_than_before=request.q_better_than_before,
            q_want_to_publish=request.q_want_to_publish,
            note=request.note,
        )
        log_path = self._episode_dir(validated) / SELF_CHECK_NAME
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as stream:
            stream.write((record.model_dump_json() + "\n").encode())
        return record.model_dump(mode="json")

    def load_self_check(self, episode_id: str) -> dict[str, object]:
        validated = validated_episode_id(episode_id)
        self._require_snapshot(validated)
        latest = latest_self_check(self._episode_dir(validated))
        if latest is None:
            return {"episode_id": validated, "self_check": None}
        return {"episode_id": validated, "self_check": latest.model_dump(mode="json")}


__all__ = ["SELF_CHECK_NAME", "SelfCheckOps", "latest_self_check"]
