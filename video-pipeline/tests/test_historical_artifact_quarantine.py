from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

from tests.historical_artifact_quarantine import (
    HISTORICAL_ARTIFACT_NODE_IDS,
    MISSING_ATTEMPT_SHA256,
    QUARANTINED_ON,
)

EXPECTED_COUNT: Final = 53
EXPECTED_NODE_ID_SHA256: Final = (
    "3f33b58a98134b62ccfc048adf47f28836f40c971d307b1260494b257114fdd5"
)


def test_historical_quarantine_exact_set_is_pinned() -> None:
    assert len(HISTORICAL_ARTIFACT_NODE_IDS) == EXPECTED_COUNT
    assert len(set(HISTORICAL_ARTIFACT_NODE_IDS)) == EXPECTED_COUNT
    payload = "\n".join(sorted(HISTORICAL_ARTIFACT_NODE_IDS)) + "\n"
    assert hashlib.sha256(payload.encode()).hexdigest() == EXPECTED_NODE_ID_SHA256


def test_historical_quarantine_metadata_names_the_missing_attempt() -> None:
    assert QUARANTINED_ON == "2026-09-02"
    assert MISSING_ATTEMPT_SHA256 == (
        "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
    )


def test_historical_quarantine_only_names_existing_test_files() -> None:
    missing = sorted(
        node_id.partition("::")[0]
        for node_id in HISTORICAL_ARTIFACT_NODE_IDS
        if not Path(node_id.partition("::")[0]).is_file()
    )
    assert not missing
