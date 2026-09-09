"""CockpitWorkspace — the composed state-access facade over EXISTING state.

Composition follows the job-runner StateStore mixin convention: one
context (``WorkspaceContext``: StateStore path + episodes root + the
snapshot guard) and four single-purpose mixins:

- ``JobOps`` — intake/list/status/publish-stub (StateStore is the
  ONLY job authority; listing reads its schema read-only);
- ``FileOps`` — brief draft, preview, review flags, review chat,
  rebuild intents (cockpit-owned files under the episode directory);
- ``ApprovalOps`` — approvals read + automation-class append through
  the append-only OperationRecordStore;
- ``ReferenceOps`` — real reference_learning ingest into one library
  file at the episodes root;
- ``ApprovalSessionsOps`` — pending approvals bundled into at most two
  normal blocking sessions (task 51);
- ``KitPreviewOps`` — task-11 kit preview manifest reads + runtime
   operator selection record (kit-previews/kit-selections files only).
- ``ChannelStyleOps`` — 工程3 explicit operator-saved channel styles
   (one channel-styles.json at the episodes root; channel-file state,
   never episode job/lock/state).

No method here may introduce a second state machine: every write goes
through an existing service API, and cockpit-owned files are
communication/intent records (brief draft, raw review messages,
rebuild intents), never job state.
"""

from __future__ import annotations

from pathlib import Path

from services.episode_cockpit.approval_sessions import ApprovalSessionsOps
from services.episode_cockpit.channel_styles import ChannelStyleOps
from services.episode_cockpit.episode_files import FileOps
from services.episode_cockpit.episode_ops import JobOps
from services.episode_cockpit.finishing_status import FinishingStatusOps
from services.episode_cockpit.kit_previews import KitPreviewOps
from services.episode_cockpit.side_desks import ApprovalOps, ReferenceOps


class CockpitWorkspace(
    JobOps,
    FileOps,
    ApprovalOps,
    ReferenceOps,
    ApprovalSessionsOps,
    KitPreviewOps,
    FinishingStatusOps,
    ChannelStyleOps,
):
    """All cockpit state access, rooted at one StateStore path + episodes root."""

    def __init__(self, *, state_store_path: Path, episodes_root: Path) -> None:
        self._state_store_path = state_store_path
        self._episodes_root = episodes_root


__all__ = ["CockpitWorkspace"]
