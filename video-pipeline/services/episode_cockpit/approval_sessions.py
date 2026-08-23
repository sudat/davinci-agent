"""Approval-session surface: bundled blocking prompts over the real ledger.

PRD 13.4 UX SLO: compatible pending approvals group into at most TWO
normal blocking review sessions. "Pending" follows the established
cockpit ledger convention (task-44 round trip): a pipeline-raised
request is recorded as an automation-class placeholder whose latest
decision is not yet an operator approval, and the operator's
``execute_approval`` supersedes it with an approve record — which clears
the session. Decided facts (latest approvals) ride along so a restarted
UI can show that acceptances survived.
"""

from __future__ import annotations

from services.approvals.store import OperationRecordStore
from services.episode_cockpit.approvals import (
    ApprovalBundle,
    bundle_approvals,
    count_blocking_sessions,
)
from services.episode_cockpit.side_desks import APPROVALS_RELATIVE
from services.episode_cockpit.workspace_context import WorkspaceContext


class ApprovalSessionsOps(WorkspaceContext):
    """Bundled approval sessions read from the append-only ledger."""

    def approval_sessions(self, episode_id: str) -> dict[str, object]:
        episode_dir = self._episode_dir(self._require_snapshot(episode_id).job.episode_id)
        records_path = episode_dir.joinpath(*APPROVALS_RELATIVE)
        if not records_path.is_file():
            return {
                "available": False,
                "sessions": [],
                "blocking_session_count": 0,
                "decided": [],
            }
        latest = OperationRecordStore(records_path).latest_records()
        pending: list[dict[str, object]] = [
            {"record_id": record.record_id, "purpose": record.purpose,
             "target_hash": record.target_hash}
            for record in sorted(latest.values(), key=lambda record: record.record_id)
            if record.decision != "approve"
        ]
        bundles = bundle_approvals(pending)
        decided: list[dict[str, object]] = [
            {"record_id": record.record_id, "purpose": record.purpose,
             "decision": record.decision}
            for record in sorted(latest.values(), key=lambda record: record.record_id)
            if record.decision == "approve"
        ]
        return {
            "available": bool(latest),
            "sessions": [_bundle_payload(bundle) for bundle in bundles.bundles],
            "blocking_session_count": count_blocking_sessions(bundles),
            "decided": decided,
        }


def _bundle_payload(bundle: ApprovalBundle) -> dict[str, object]:
    return {
        "session_key": bundle.session_key,
        "kind": bundle.kind,
        "purposes": list(bundle.purposes),
        "items": [dict(item.model_dump(mode="json")) for item in bundle.items],
        "explanation": bundle.explanation,
    }


__all__ = ["ApprovalSessionsOps"]
