"""Compatibility facade: the samples cluster now lives in two modules.

P2-1 split: (a) sample_identity.py — identity/manifest/journal models,
digests, lineage derivation; (b) sample_journal.py — journal
append/recovery/idempotency. Import from those modules directly in new
code; this facade re-exports the public surface so existing importers
keep working.
"""

from __future__ import annotations

from services.episode_cockpit.sample_complete import (
    complete_missing_budget_settle_locked,
    has_preview_settle_locked,
    verify_recovery,
    write_published_manifest,
)
from services.episode_cockpit.sample_identity import (
    SAMPLE_MANIFEST_NAME,
    SAMPLE_PREVIEW_NAME,
    SAMPLE_RENDER_LOCK_NAME,
    SAMPLES_DIR_NAME,
    SAMPLES_JOURNAL_NAME,
    LineageSequence,
    SampleEventKind,
    SampleJournalEventV1,
    SampleLineageEntryV1,
    SampleManifestV1,
    SampleRequestIdentityV1,
    SampleStatus,
    WindowSequence,
    derive_sample_id,
    derive_sample_lineage,
    normalized_windows,
    render_lock_path,
    sample_dir,
    sample_identity_digest,
    sample_ir_sha256,
    samples_root,
    verify_sample_content,
)
from services.episode_cockpit.sample_journal import (
    complete_missing_success_locked,
    load_sample_events,
    record_sample_success,
    record_sample_success_locked,
    request_sample,
    request_sample_locked,
    sample_success_journaled_locked,
    settle_sample_failed,
    settle_sample_failed_locked,
)

__all__: list[str] = [
    "SAMPLES_DIR_NAME",
    "SAMPLES_JOURNAL_NAME",
    "SAMPLE_MANIFEST_NAME",
    "SAMPLE_PREVIEW_NAME",
    "SAMPLE_RENDER_LOCK_NAME",
    "LineageSequence",
    "SampleEventKind",
    "SampleJournalEventV1",
    "SampleLineageEntryV1",
    "SampleManifestV1",
    "SampleRequestIdentityV1",
    "SampleStatus",
    "WindowSequence",
    "complete_missing_budget_settle_locked",
    "complete_missing_success_locked",
    "derive_sample_id",
    "derive_sample_lineage",
    "has_preview_settle_locked",
    "load_sample_events",
    "normalized_windows",
    "record_sample_success",
    "record_sample_success_locked",
    "render_lock_path",
    "request_sample",
    "request_sample_locked",
    "sample_dir",
    "sample_identity_digest",
    "sample_ir_sha256",
    "sample_success_journaled_locked",
    "samples_root",
    "settle_sample_failed",
    "settle_sample_failed_locked",
    "verify_recovery",
    "verify_sample_content",
    "write_published_manifest",
]
