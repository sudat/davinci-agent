from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import services.artifact_registry.registry as registry_module
from services.artifact_registry.models import RegistryIndex, index_seal
from services.artifact_registry.registry import RegistryError
from services.foundation_io import canonical_model_bytes
from tests.artifact_registry.support import make_registry, make_store, publish

WRONG_SHA256 = "f" * 64


def test_malformed_index_json_rejected(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    registry.index_path.write_bytes(b"{not-json")

    with pytest.raises(RegistryError) as error:
        registry.load()

    assert error.value.code == "index-invalid"


def test_schema_invalid_index_rejected(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    registry.index_path.write_bytes(
        b'{"schema_version":"registry-index-v1","unexpected":true}'
    )

    with pytest.raises(RegistryError) as error:
        registry.load()

    assert error.value.code == "index-invalid"


def test_unsealed_tamper_rejected(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    receipt = publish(store, "art-a", b"payload-a")
    registry.register(store, receipt)

    raw = json.loads(registry.index_path.read_text())
    raw["entries"]["art-a"]["content_sha256"] = WRONG_SHA256
    registry.index_path.write_text(
        json.dumps(raw, sort_keys=True, separators=(",", ":"))
    )

    with pytest.raises(RegistryError) as error:
        registry.load()

    assert error.value.code == "index-invalid"
    assert "seal" in error.value.detail


def test_by_sha256_inconsistency_rejected_even_when_sealed(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    receipt = publish(store, "art-a", b"payload-a")
    entry = registry.register(store, receipt)

    missing_map: dict[str, tuple[str, ...]] = {}
    with pytest.raises(ValidationError):
        RegistryIndex.model_validate(
            {
                "schema_version": "registry-index-v1",
                "entries": {"art-a": entry},
                "by_sha256": missing_map,
                "index_sha256": index_seal({"art-a": entry}, missing_map),
            }
        )

    unknown_id_map = {entry.content_sha256: ("art-a", "art-ghost")}
    with pytest.raises(ValidationError):
        RegistryIndex.model_validate(
            {
                "schema_version": "registry-index-v1",
                "entries": {"art-a": entry},
                "by_sha256": unknown_id_map,
                "index_sha256": index_seal({"art-a": entry}, unknown_id_map),
            }
        )


def test_atomic_index_crash_sim_is_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    receipt_a = publish(store, "art-a", b"payload-a")
    registry.register(store, receipt_a)
    before = registry.index_path.read_bytes()

    def exploding_write(path: Path, payload: bytes) -> None:
        partial = path.parent / f".{path.name}.crash-sim"
        partial.write_bytes(payload[: len(payload) // 2])
        raise OSError("simulated crash between temp write and rename")

    monkeypatch.setattr(registry_module, "atomic_write", exploding_write)
    receipt_b = publish(store, "art-b", b"payload-b")
    with pytest.raises(OSError, match="simulated crash"):
        registry.register(store, receipt_b)

    assert registry.index_path.read_bytes() == before
    assert RegistryIndex.model_validate_json(registry.index_path.read_bytes())

    monkeypatch.undo()
    registry.register(store, receipt_b)
    assert set(registry.load().entries) == {"art-a", "art-b"}
    assert canonical_model_bytes(registry.load())
