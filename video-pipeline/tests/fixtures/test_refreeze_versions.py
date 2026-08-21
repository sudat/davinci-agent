"""Same-gate re-freeze support: version-parameterized freeze preparation.

The v1 tooling hard-refused any re-freeze of a gate (same-version guard)
with no way to freeze a corrected v2 policy after a blocker fix. The
prepare modules now accept an explicit gate version; the child policy,
receipt, and intent all carry it, parent verification derives the parent
policy path from the parent result's own version, and the same-version
guard still refuses overwriting an existing version.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from services.execution.preflight import ExecutionContract
from services.fixtures.models import FreezeIntent
from services.fixtures.prepare import PrepareError, PrepareRequest, prepare_freeze
from services.foundation_io import canonical_model_bytes
from services.gates import GatePolicy
from services.gates.serialization import canonical_gate_bytes
from services.source_snapshot import SourceSnapshot

ATTEMPT = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
CONTRACT = ATTEMPT / "execution-contract.json"


def _canonical_contract(tmp_path: Path) -> Path:
    out = tmp_path / "execution-contract.canonical.json"
    out.write_bytes(
        canonical_model_bytes(ExecutionContract.model_validate_json(CONTRACT.read_bytes()))
    )
    return out


def _snapshot(tmp_path: Path) -> Path:
    out = tmp_path / "pre-source.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "services.source_snapshot",
            "--root",
            str(Path.cwd()),
            "--execution-contract",
            str(_canonical_contract(tmp_path)),
            "--out",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    SourceSnapshot.model_validate_json(out.read_bytes())
    return out


def _request(tmp_path: Path, policy_out: Path, version: str) -> PrepareRequest:
    return PrepareRequest(
        toolchain_lock=Path("config/toolchains/phase-0a-v1.json"),
        fixture_ids=("p0a-cfr30-fixed",),
        pre_source_snapshot=_snapshot(tmp_path),
        execution_contract=_canonical_contract(tmp_path),
        policy_out=policy_out,
        freeze_receipt=tmp_path / "receipt.json",
        staging=tmp_path / "staging",
        intent=tmp_path / "intent.json",
        gate_version=version,  # type: ignore[arg-type]
    )


def test_phase0a_v2_refreeze_preparation(tmp_path: Path) -> None:
    policy_out = Path.cwd() / "config/gates/phase-0a-v2-refreeze-test.json"
    request = _request(tmp_path, policy_out, "v2")
    intent = prepare_freeze(request)

    assert isinstance(intent, FreezeIntent)
    assert intent.gate_id == "phase-0a"
    assert intent.gate_version == "v2"
    staged_policy = Path(intent.staged_policy_path)
    policy = GatePolicy.model_validate_json(staged_policy.read_bytes())
    assert policy.gate_version == "v2"
    assert staged_policy.read_bytes() == canonical_gate_bytes(policy)
    assert not policy_out.exists()
    receipt_raw = Path(intent.staged_receipt_path).read_bytes()
    assert b'"gate_version":"v2"' in receipt_raw


def test_same_version_refreeze_still_refused(tmp_path: Path) -> None:
    existing = tmp_path / "phase-0a-v1.json"
    existing.write_text("{}")
    request = _request(tmp_path, existing, "v1")
    with pytest.raises(PrepareError, match="same-version re-freeze is forbidden"):
        prepare_freeze(request)


def test_v1_policy_path_stays_refused(tmp_path: Path) -> None:
    policy_out = Path.cwd() / "config/gates/phase-0a-v1.json"
    request = _request(tmp_path, policy_out, "v1")
    with pytest.raises(PrepareError, match="same-version re-freeze is forbidden"):
        prepare_freeze(request)
