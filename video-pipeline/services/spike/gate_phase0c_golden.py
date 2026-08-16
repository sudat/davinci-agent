"""Frozen Golden table loading for the Phase-0C gate.

Binds the policy to the golden index (sha256), the index to ``expected.json``
and to every per-fixture manifest hash, then parses each golden case into the
strict ``GoldenCase0C`` projection used by the recomputation checks. Any drift
raises ``GoldenLoadError`` before a single criterion is evaluated.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal, cast

from services.foundation_io import sha256_file
from services.spike.gate_phase0c_checks import (
    GoldenCase0C,
    GoldenPlanItem,
    GoldenRecordRow,
)
from services.spike.gate_phase0c_models import (
    GOLDEN_DIR,
    GOLDEN_EXPECTED_NAME,
    GOLDEN_INDEX_NAME,
    fixture_manifest_path,
)

if TYPE_CHECKING:
    from services.gates import GatePolicy


class GoldenLoadError(Exception):
    """The frozen Golden index/table is missing, noncanonical, or unbound."""


def _kind(raw: dict[str, object]) -> Literal["video", "audio", "subtitle"]:
    value = raw.get("kind")
    if value not in ("video", "audio", "subtitle"):
        raise GoldenLoadError(f"golden kind {value!r} is not a track kind")
    return cast("Literal['video', 'audio', 'subtitle']", value)


def _golden_item(raw: object) -> GoldenPlanItem:
    if not isinstance(raw, dict):
        raise GoldenLoadError("golden plan item is not an object")
    span = raw.get("span")
    if not isinstance(span, dict):
        raise GoldenLoadError("golden plan item lacks a span")
    return GoldenPlanItem(
        item_id=str(raw["item_id"]),
        kind=_kind(raw),
        track_index=int(raw["track_index"]),
        av_link_id=cast("str | None", raw.get("av_link_id")),
        subtitle_text=cast("str | None", raw.get("subtitle_text")),
        span_start=int(span["start_frame"]),
        span_end=int(span["end_frame"]),
    )


def _golden_row(raw: object) -> GoldenRecordRow:
    if not isinstance(raw, dict):
        raise GoldenLoadError("golden record row is not an object")
    return GoldenRecordRow(
        item_id=str(raw["item_id"]),
        kind=_kind(raw),
        track_index=int(raw["track_index"]),
        record_start=int(raw["record_start"]),
        record_end=int(raw["record_end"]),
        source_start=int(raw["source_start"]),
        source_end=int(raw["source_end"]),
        subtitle_text=cast("str | None", raw.get("subtitle_text")),
    )


def _literal(raw: dict[str, object], key: str, allowed: tuple[str, ...]) -> str:
    value = raw.get(key)
    if value not in allowed:
        raise GoldenLoadError(f"golden {key} {value!r} is not one of {allowed}")
    return str(value)


def _golden_case(fixture_id: str, raw: object) -> GoldenCase0C:
    if not isinstance(raw, dict):
        raise GoldenLoadError(f"golden case {fixture_id} is malformed")
    return GoldenCase0C(
        classification=cast(
            "Literal['clear', 'ambiguous', 'conflict']",
            _literal(raw, "classification", ("clear", "ambiguous", "conflict")),
        ),
        decision=cast(
            "Literal['apply', 'defer']", _literal(raw, "decision", ("apply", "defer"))
        ),
        resulting_plan_version=cast(
            "Literal['v1', 'v2']", _literal(raw, "resulting_plan_version", ("v1", "v2"))
        ),
        target_candidate_item_ids=tuple(str(item) for item in raw["target_candidate_item_ids"]),
        plan_items=tuple(_golden_item(item) for item in raw["plan_items"]),
        record_table=tuple(_golden_row(row) for row in raw["record_table"]),
    )


def _verify_manifest_bindings(index: dict[str, object], policy: GatePolicy) -> None:
    combined = hashlib.sha256()
    bindings = index.get("fixture_manifest_sha256s")
    if not isinstance(bindings, dict):
        raise GoldenLoadError("golden index lacks fixture manifest bindings")
    fixture_ids = index.get("fixture_ids")
    if not isinstance(fixture_ids, list):
        raise GoldenLoadError("golden index lacks the canonical fixture order")
    for fixture_id in fixture_ids:
        bound = bindings.get(str(fixture_id))
        if bound is None:
            raise GoldenLoadError(f"golden index lacks a binding for {fixture_id}")
        if sha256_file(fixture_manifest_path(str(fixture_id))) != bound:
            raise GoldenLoadError(f"fixture manifest {fixture_id} drifted from the golden index")
        combined.update(fixture_manifest_path(str(fixture_id)).read_bytes())
    if combined.hexdigest() != policy.fixture_manifest_sha256:
        raise GoldenLoadError("combined fixture-manifest hash drift vs policy")


def load_golden0c(policy: GatePolicy) -> dict[str, GoldenCase0C]:
    index_raw = (GOLDEN_DIR / GOLDEN_INDEX_NAME).read_bytes()
    if hashlib.sha256(index_raw).hexdigest() != policy.golden_sha256:
        raise GoldenLoadError("golden index hash does not match the policy binding")
    index = json.loads(index_raw)
    if not isinstance(index, dict):
        raise GoldenLoadError("golden index is malformed")
    expected_path = GOLDEN_DIR / GOLDEN_EXPECTED_NAME
    if sha256_file(expected_path) != index.get("expected_sha256"):
        raise GoldenLoadError("golden expected.json does not match the index binding")
    _verify_manifest_bindings(index, policy)
    payload = json.loads(expected_path.read_bytes())
    fixtures = payload.get("fixtures") if isinstance(payload, dict) else None
    if not isinstance(fixtures, dict):
        raise GoldenLoadError("golden expected table is malformed")
    return {
        str(fixture_id): _golden_case(str(fixture_id), raw) for fixture_id, raw in fixtures.items()
    }


__all__ = [
    "GoldenLoadError",
    "load_golden0c",
]
