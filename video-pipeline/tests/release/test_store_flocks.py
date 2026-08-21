"""Blocker-fix regression: authoritative stores serialize under flock.

The approvals chain append (read+verify+sequence+supersession+append)
and the registry register (load-modify-replace) were unlocked, so two
concurrent writers could corrupt the record chain or silently lose a
registry entry. Both boundaries now hold an exclusive flock across the
whole read-modify-write. The interleaving is forced deterministically
by widening the read-to-write window; a two-process variant re-proves
the same invariant without any monkeypatching.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import services.approvals.store as approvals_store_module
import services.artifact_registry.registry as registry_module
from services.approvals.models import OperationDraft
from services.approvals.store import OperationRecordStore
from services.artifact_registry.registry import ArtifactRegistry
from services.artifact_store.models import PublicationIntent
from services.artifact_store.store import ArtifactStore
from services.contracts.primitives import ArtifactEnvelope, Producer

WIDEN_SECONDS = 0.15

REGISTER_SNIPPET = textwrap.dedent(
    """
    import hashlib
    import sys
    import time
    from pathlib import Path

    from services.artifact_registry.registry import ArtifactRegistry
    from services.artifact_store.models import ArtifactEnvelope, PublicationIntent
    from services.artifact_store.store import ArtifactStore
    from services.contracts.primitives import Producer

    store_root = Path(sys.argv[1])
    index_root = Path(sys.argv[2])
    marker = Path(sys.argv[3])
    label = sys.argv[4]
    deadline = time.time() + 30
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.01)
    store = ArtifactStore(store_root)
    registry = ArtifactRegistry(index_root)
    payload = label.encode()
    intent = PublicationIntent(envelope=ArtifactEnvelope(
        artifact_id="race-" + label,
        artifact_type="race-artifact",
        schema_version="race-v1",
        content_hash=hashlib.sha256(payload).hexdigest(),
        producer=Producer(name="race", version="v1"),
        inputs=(),
    ))
    receipt = store.publish(intent, payload)
    registry.register(store, receipt)
    """
)


@contextmanager
def widened_read(module: object, attribute: str) -> Iterator[None]:
    """Sleep after the original read returns, widening the RMW race window."""
    original = getattr(module, attribute)
    if not callable(original):
        raise TypeError(f"{attribute} is not callable")

    def widened(*arguments: object, **keywords: object) -> object:
        result = original(*arguments, **keywords)
        time.sleep(WIDEN_SECONDS)
        return result

    setattr(module, attribute, widened)
    try:
        yield
    finally:
        setattr(module, attribute, original)


def draft_for(label: str) -> OperationDraft:
    return OperationDraft.model_validate(
        {
            "purpose": "manual_freeze",
            "target_type": "frozen-timeline",
            "target_hash": hashlib.sha256(label.encode()).hexdigest(),
            "decision": "approve",
            "actor_id": f"proc-{label}",
            "runner_class": "automation",
            "fixture_only": True,
        }
    )


def test_interleaved_approval_appends_keep_a_valid_chain(tmp_path: Path) -> None:
    records_path = tmp_path / "shared" / "operation-records.jsonl"
    OperationRecordStore(records_path).append(draft_for("seed"))
    failures: list[str] = []

    def append(label: str) -> None:
        try:
            OperationRecordStore(records_path).append(draft_for(label))
        except Exception as error:  # noqa: BLE001 (recorded, never silently dropped)
            failures.append(f"{label}:{type(error).__name__}")

    with widened_read(approvals_store_module, "_read_records"):
        threads = [
            threading.Thread(target=append, args=(f"racer-{index}",)) for index in range(3)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert failures == []
    records = OperationRecordStore(records_path).verify_chain()
    assert len(records) == 4
    assert len({record.record_id for record in records}) == 4


def _intent(label: str, payload: bytes) -> PublicationIntent:
    return PublicationIntent(
        envelope=ArtifactEnvelope(
            artifact_id=label,
            artifact_type="race-artifact",
            schema_version="race-v1",
            content_hash=hashlib.sha256(payload).hexdigest(),
            producer=Producer(name="race", version="v1"),
            inputs=(),
        )
    )


def test_interleaved_registry_registration_loses_no_entries(tmp_path: Path) -> None:
    artifact_store = ArtifactStore(tmp_path / "artifact-store")
    registry = ArtifactRegistry(tmp_path / "registry")
    _seed(artifact_store, "seed-a")
    _seed(artifact_store, "seed-b")

    def register(label: str) -> None:
        payload = label.encode()
        receipt = artifact_store.publish(_intent(label, payload), payload)
        registry.register(artifact_store, receipt)

    with widened_read(registry_module.ArtifactRegistry, "load"):
        threads = [
            threading.Thread(target=register, args=(f"racer-{index}",)) for index in range(3)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    entries = registry.load().entries
    assert {f"racer-{index}" for index in range(3)} <= set(entries)


def test_two_process_registry_registration_loses_no_entries(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "registry")
    marker = tmp_path / "start.marker"
    workers = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                REGISTER_SNIPPET,
                str(tmp_path / "artifact-store"),
                str(tmp_path / "registry"),
                str(marker),
                label,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for label in ("race-a", "race-b")
    ]
    marker.write_text("go")
    for worker in workers:
        _out, err = worker.communicate(timeout=60)
        assert worker.returncode == 0, err.decode()
    entries = registry.load().entries
    assert {"race-race-a", "race-race-b"} <= set(entries)


def _seed(store: ArtifactStore, label: str) -> None:
    payload = label.encode()
    store.publish(_intent(label, payload), payload)
