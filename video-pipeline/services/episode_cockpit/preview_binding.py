"""Preview-version binding: is the served preview the current run's video?

Read-only derivation over EXISTING evidence (honest absence): the latest
``preview_published`` journal line in runner.log, the succeeded ``preview``
stage rows in the cockpit StateStore, and the chain review bundle under
``run/``. A binding is produced ONLY when all three agree (same run, same
plan version, same content hash); absent, old-format, or corrupt evidence
degrades to ``None`` — the caller then serves the video WITHOUT the
``X-Cockpit-Preview-*`` headers (unknown), never an error and never a
partial header set. Runtime-state reads only; no artifact is written.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from pydantic import ValidationError

from services.contracts.primitives import Identifier, Sha256, StrictModel

if TYPE_CHECKING:
    from collections.abc import Sequence

    from services.job_runner.state_models import StageRunRow

PUBLISH_EVENT: Final = "preview_published"
RUNNER_LOG_NAME: Final = "runner.log"
_RUN_BUNDLE_RELATIVE: Final = ("run", "review-bundle.json")
_TAIL_BYTES: Final = 65536

HEADER_RUN_ID: Final = "X-Cockpit-Preview-Run-Id"
HEADER_TARGET_VERSION: Final = "X-Cockpit-Preview-Target-Version"
HEADER_CONTENT_SHA256: Final = "X-Cockpit-Preview-Content-SHA256"
HEADER_OUTPUT_ARRIVED_AT: Final = "X-Cockpit-Preview-Output-Arrived-At"


class _PublishRecord(StrictModel):
    """Parse-once shape of the enriched ``preview_published`` journal line.

    Every field is required: an old-format line (no binding fields yet) or
    a corrupt one (non-hex hash, bad run id) fails validation and reads as
    unknown — it can never half-bind a response.
    """

    event: Literal["preview_published"]
    ts: str
    path: str
    source: str
    run_id: Identifier
    target_version: str
    content_hash: Sha256


class PreviewBinding(StrictModel):
    """The verified run/version binding of the currently served preview."""

    run_id: Identifier
    target_version: str
    content_hash: Sha256
    output_arrived_at: str
    output_id: Literal["landscape", "vertical"] = "landscape"


def latest_publish_record(runner_log: Path) -> dict[str, object] | None:
    """The newest ``preview_published`` line in a bounded tail (None = absent)."""

    try:
        size = runner_log.stat().st_size
        with runner_log.open("rb") as stream:
            if size > _TAIL_BYTES:
                stream.seek(size - _TAIL_BYTES)
            payload = stream.read()
    except OSError:
        return None
    for line in reversed(payload.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue  # a tail-cut partial first line is skipped, not fatal
        if isinstance(event, dict) and event.get("event") == PUBLISH_EVENT:
            return event
    return None


def published_target_version(runner_log: Path) -> str | None:
    """The latest COMPLETE publish record's target version (None = unknown)."""

    record = latest_publish_record(runner_log)
    if record is None:
        return None
    try:
        return _PublishRecord.model_validate(record).target_version
    except ValidationError:
        return None


def derive_preview_binding(
    episode_dir: Path,
    stage_runs: Sequence[StageRunRow],
    output_id: Literal["landscape", "vertical"] = "landscape",
) -> PreviewBinding | None:
    """Bind the served preview to run+version ONLY on the full cross-check.

    All three must agree: the latest publish record is complete; a SUCCEEDED
    ``preview`` stage row exists with the SAME run and the SAME adopted
    hash; and the chain bundle ``current`` names the same version and hash.
    Anything else is None — unknown, never partially bound. The binding is
    stamped with the requested ``output_id`` so evidence names which
    canvas it describes.
    """

    record = latest_publish_record(episode_dir / RUNNER_LOG_NAME)
    if record is None:
        return None
    try:
        parsed = _PublishRecord.model_validate(record)
    except ValidationError:
        return None
    if not any(
        run.stage_name == "preview"
        and run.status == "succeeded"
        and run.run_id == parsed.run_id
        and run.adopted_artifact_hash == parsed.content_hash
        for run in stage_runs
    ):
        return None
    if _bundle_current(episode_dir) != (parsed.target_version, parsed.content_hash):
        return None
    return PreviewBinding(
        run_id=parsed.run_id,
        target_version=parsed.target_version,
        content_hash=parsed.content_hash,
        output_arrived_at=parsed.ts,
        output_id=output_id,
    )


def _bundle_current(episode_dir: Path) -> tuple[str, str] | None:
    """(plan_version, preview_sha256) of the chain bundle ``current`` view.

    Local boundary parse mirroring services/cli/bundle.py — kept inline so
    services/ does not depend on cli/ (dependency-direction guard); an
    absent or unreadable bundle is None, never an error.
    """

    try:
        payload = json.loads(episode_dir.joinpath(*_RUN_BUNDLE_RELATIVE).read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    current = payload.get("current")
    if not isinstance(current, dict):
        return None
    version = current.get("plan_version")
    preview_sha = current.get("preview_sha256")
    if not isinstance(version, str) or not isinstance(preview_sha, str):
        return None
    return version, preview_sha


def binding_headers(binding: PreviewBinding) -> dict[str, str]:
    """The four response headers the cockpit adds on a verified binding."""

    return {
        HEADER_RUN_ID: binding.run_id,
        HEADER_TARGET_VERSION: binding.target_version,
        HEADER_CONTENT_SHA256: binding.content_hash,
        HEADER_OUTPUT_ARRIVED_AT: binding.output_arrived_at,
    }


__all__ = [
    "HEADER_CONTENT_SHA256",
    "HEADER_OUTPUT_ARRIVED_AT",
    "HEADER_RUN_ID",
    "HEADER_TARGET_VERSION",
    "PUBLISH_EVENT",
    "PreviewBinding",
    "binding_headers",
    "derive_preview_binding",
    "latest_publish_record",
    "published_target_version",
]
