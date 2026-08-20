"""Convert raw review-work / debugging lane responses to Global Review v1.

Raw contract (markdown or JSON): the response must declare ``lane``,
``full_sha``, ``candidate_id``, a terminal ``verdict``, and the mandatory
``STOP`` instruction line/field (audit stop directive; omitted → typed
failure). Only a raw PASS with no blockers maps to APPROVE; FAIL /
INCONCLUSIVE map to REJECT; a claimed PASS with blockers is a typed
``blockers-present`` failure (misleading raw evidence never converts).
Bindings must match the declared lane/SHA/candidate exactly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Final, NoReturn

from pydantic import TypeAdapter, ValidationError

from services.approvals.global_review import GlobalReviewError
from services.approvals.global_review_models import (
    REVIEW_LANES,
    GlobalReviewReport,
    GlobalReviewVerdict,
    ReviewLane,
)
from services.foundation_io import atomic_write, canonical_model_bytes

PASS_TOKENS: Final = {"PASS", "PASSED", "APPROVE"}
FAIL_TOKENS: Final = {"FAIL", "FAILED", "REJECT", "CHANGES_REQUESTED", "INCONCLUSIVE"}
_ARTIFACT_SPLIT: Final = "="
_SHA256_HEX_LENGTH: Final = 64
_LANE_ADAPTER: Final[TypeAdapter[ReviewLane]] = TypeAdapter(ReviewLane)
_VERDICT_ADAPTER: Final[TypeAdapter[GlobalReviewVerdict]] = TypeAdapter(GlobalReviewVerdict)
_FIELD_KEYS: Final = frozenset(
    {"lane", "full-sha", "candidate-id", "verdict", "stop", "finding", "artifact", "blocker"}
)


class ReviewConvertError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class _CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ReviewConvertError("cli-arguments", message)


def _normalize_key(value: str) -> str:
    return value.strip().lstrip("#*_> \t").lower().replace("_", "-").replace(" ", "-")


def _normalize_verdict(value: str) -> str:
    return value.strip().strip("*_`").upper()


def _parse_markdown(raw: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        normalized = _normalize_key(key)
        if normalized == "overall-verdict":
            normalized = "verdict"
        if normalized in _FIELD_KEYS:
            fields.setdefault(normalized, []).append(value.strip())
    return fields


def _single_field(fields: dict[str, list[str]], key: str) -> str:
    values = fields.get(key, [])
    if not values:
        raise ReviewConvertError(
            "malformed-raw", f"raw response omits '{key.replace('-', '_')}'"
        )
    if len(values) > 1:
        raise ReviewConvertError("malformed-raw", f"duplicate '{key}' declaration")
    return values[0]


def _check_bindings(single: dict[str, str], *, lane: str, full_sha: str, candidate_id: str) -> None:
    if _normalize_key(single["lane"]) != lane:
        raise ReviewConvertError(
            "lane-mismatch", f"raw declares lane {single['lane']!r}, expected {lane!r}"
        )
    if single["full-sha"].lower() != full_sha.lower():
        raise ReviewConvertError(
            "sha-mismatch", f"raw binds {single['full-sha']}, not the declared {full_sha}"
        )
    if single["candidate-id"] != candidate_id:
        raise ReviewConvertError(
            "candidate-mismatch",
            f"raw binds {single['candidate-id']}, not the declared {candidate_id}",
        )


def _resolve_verdict(fields: dict[str, list[str]]) -> tuple[str, list[str]]:
    verdicts = fields.get("verdict", [])
    if len(verdicts) != 1:
        raise ReviewConvertError("verdict-missing", "raw response must state exactly one verdict")
    stops = fields.get("stop", [])
    if len(stops) != 1 or not stops[0].strip():
        raise ReviewConvertError(
            "stop-instruction-omitted",
            "raw reviewer responses must carry the mandatory STOP instruction",
        )
    verdict_token = _normalize_verdict(verdicts[0])
    blockers = [item for item in fields.get("blocker", []) if item.strip()]
    if verdict_token in PASS_TOKENS:
        if blockers:
            raise ReviewConvertError(
                "blockers-present",
                f"raw claims PASS but lists blockers: {blockers}; only PASS with no "
                "blockers maps to APPROVE",
            )
        return "APPROVE", blockers
    if verdict_token in FAIL_TOKENS:
        return "REJECT", blockers
    raise ReviewConvertError("malformed-raw", f"unrecognized verdict token {verdict_token!r}")


def _artifact_hashes(fields: dict[str, list[str]]) -> dict[str, str]:
    artifact_hashes: dict[str, str] = {}
    for entry in fields.get("artifact", []):
        name, separator, digest = entry.partition(_ARTIFACT_SPLIT)
        if not separator or not name.strip() or len(digest) != _SHA256_HEX_LENGTH:
            raise ReviewConvertError(
                "malformed-raw", f"artifact line must be 'name=<64-hex sha256>': {entry!r}"
            )
        artifact_hashes[name.strip()] = digest.strip().lower()
    return artifact_hashes


def convert_raw_review(
    raw: bytes, *, lane: str, full_sha: str, candidate_id: str
) -> GlobalReviewReport:
    if lane not in REVIEW_LANES:
        raise ReviewConvertError("malformed-raw", f"unknown lane {lane!r}")
    text = raw.decode("utf-8", errors="strict")
    fields = _parse_json_raw(text) if text.lstrip().startswith("{") else _parse_markdown(text)
    single = {key: _single_field(fields, key) for key in ("lane", "full-sha", "candidate-id")}
    _check_bindings(single, lane=lane, full_sha=full_sha, candidate_id=candidate_id)
    verdict, blockers = _resolve_verdict(fields)
    findings = tuple(item for item in fields.get("finding", []) if item.strip())
    try:
        return GlobalReviewReport(
            lane=_LANE_ADAPTER.validate_python(lane),
            full_sha=full_sha,
            candidate_id=candidate_id,
            verdict=_VERDICT_ADAPTER.validate_python(verdict),
            raw_response_sha256=hashlib.sha256(raw).hexdigest(),
            artifact_hashes=_artifact_hashes(fields),
            findings=(*findings, *blockers) if blockers else findings,
        )
    except ValidationError as error:
        raise ReviewConvertError(
            "malformed-raw", f"converted report is invalid: {error}"
        ) from error


def _parse_json_raw(text: str) -> dict[str, list[str]]:
    try:
        payload: object = json.loads(text)
    except json.JSONDecodeError as error:
        raise ReviewConvertError("malformed-raw", f"raw JSON does not parse: {error}") from error
    if not isinstance(payload, dict):
        raise ReviewConvertError("malformed-raw", "raw JSON must be an object")
    allowed = {
        "lane", "full_sha", "candidate_id", "verdict", "stop", "findings",
        "artifact_hashes", "blockers",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ReviewConvertError("malformed-raw", f"unknown raw JSON keys: {unknown}")
    fields: dict[str, list[str]] = {}
    for key in ("lane", "full_sha", "candidate_id", "verdict"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ReviewConvertError("malformed-raw", f"raw JSON field {key!r} must be a string")
        fields[key.replace("_", "-")] = [value.strip()]
    stop = payload.get("stop")
    if not isinstance(stop, str) or not stop.strip():
        raise ReviewConvertError(
            "stop-instruction-omitted",
            "raw reviewer responses must carry the mandatory STOP instruction",
        )
    fields["stop"] = [stop.strip()]
    findings = payload.get("findings", [])
    if not isinstance(findings, list) or any(not isinstance(item, str) for item in findings):
        raise ReviewConvertError("malformed-raw", "'findings' must be a list of strings")
    fields["finding"] = findings
    blockers = payload.get("blockers", [])
    if not isinstance(blockers, list) or any(not isinstance(item, str) for item in blockers):
        raise ReviewConvertError("malformed-raw", "'blockers' must be a list of strings")
    fields["blocker"] = blockers
    artifacts = payload.get("artifact_hashes", {})
    if not isinstance(artifacts, dict):
        raise ReviewConvertError("malformed-raw", "'artifact_hashes' must be an object")
    fields["artifact"] = [f"{name}={digest}" for name, digest in artifacts.items()]
    return fields


def _parser() -> argparse.ArgumentParser:
    parser = _CliParser(prog="python -m services.cli convert-review")
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--lane", required=True)
    parser.add_argument("--full-sha", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        raw = arguments.raw.read_bytes()
        converted = convert_raw_review(
            raw,
            lane=arguments.lane,
            full_sha=arguments.full_sha,
            candidate_id=arguments.candidate_id,
        )
        atomic_write(arguments.out, canonical_model_bytes(converted))
    except (OSError, ReviewConvertError, GlobalReviewError) as error:
        print(f"convert_failed: {error}", file=sys.stderr)
        return 1
    print(
        f"converted: {arguments.out} lane={converted.lane} "
        f"verdict={converted.verdict} candidate={converted.candidate_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
