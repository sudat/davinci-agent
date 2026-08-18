"""Deterministic Candidate ID hashing (Todo 40).

``compute_candidate_id`` derives a stable sha256 over the CANONICAL
serialization of the normalized identity fields — edit-source sha, source id,
half-open span (frames + rational rate), intent, analyzer version. The same
content always yields the same ID across runs and processes; anything else
(evidence, provenance details, handles) is deliberately OUTSIDE the identity
payload and is guarded by the identity ledger in :mod:`services.editorial.reconcile`.
The LLM never participates: IDs are assigned by this module only.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from services.contracts.primitives import Sha256

CANONICAL_SPAN_FIELDS: Final = ("end_frame", "rate_den", "rate_num", "start_frame")


class CandidateIdentityConflict(Exception):  # noqa: N818 (mirrors ApiBudgetExceeded)
    """Two candidates normalized to one ID but carry different content."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def canonical_identity_payload(  # noqa: PLR0913 (identity fields are the record)
    *,
    edit_source_sha: str,
    source_id: str,
    start_frame: int,
    end_frame: int,
    rate_num: int,
    rate_den: int,
    intent: str,
    analyzer_version: str,
) -> bytes:
    """Canonical (sorted, compact, utf-8) serialization of the identity fields."""

    payload = {
        "analyzer_version": analyzer_version,
        "intent": intent,
        "source_id": source_id,
        "source_sha": edit_source_sha,
        "span": {
            "end_frame": end_frame,
            "rate_den": rate_den,
            "rate_num": rate_num,
            "start_frame": start_frame,
        },
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_candidate_id(  # noqa: PLR0913 (identity fields are the record)
    *,
    edit_source_sha: str,
    source_id: str,
    start_frame: int,
    end_frame: int,
    rate_num: int,
    rate_den: int,
    intent: str,
    analyzer_version: str,
) -> Sha256:
    """STABLE deterministic ID over the normalized candidate identity."""

    canonical = canonical_identity_payload(
        edit_source_sha=edit_source_sha,
        source_id=source_id,
        start_frame=start_frame,
        end_frame=end_frame,
        rate_num=rate_num,
        rate_den=rate_den,
        intent=intent,
        analyzer_version=analyzer_version,
    )
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "CANONICAL_SPAN_FIELDS",
    "CandidateIdentityConflict",
    "canonical_identity_payload",
    "compute_candidate_id",
]
