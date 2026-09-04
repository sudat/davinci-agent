"""Finishing domains status: read-only projection of finishing-run.json.

The file at ``<episode>/finishing/finishing-run.json`` is written by
``services.cli.v44_finishing`` (runtime report, NOT authoritative). This mixin
only READS the three display fields
(``domain_statuses`` / ``domain_justifications`` / ``blocked_domains``).
Missing file → honest ``available: false`` stub (never an error dump);
malformed file → typed 422 ``finishing-run-invalid`` (precedent:
``kit-manifest-invalid`` in ``kit_previews.py``).

Dependency-direction guard: the canonical seven domain names and the
``finishing/finishing-run.json`` relative path mirror the CLI constants
(``services.cli._v44_finishing_report.QUALITY_DOMAIN_NAMES`` /
``services.cli._v44_finishing_record.FINISHING_REPORT_RELATIVE``) inline so
``services/`` does not depend on ``services.cli`` — same precedent as
``episode_files.py``'s mirrored runner constants.
"""

from __future__ import annotations

import json
from typing import Annotated, Final

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from services.contracts.primitives import to_tuple
from services.episode_cockpit.errors import CockpitUnprocessableError
from services.episode_cockpit.workspace_context import WorkspaceContext

# Mirror of services/cli/_v44_finishing_report.py — keep in sync.
QUALITY_DOMAIN_NAMES: Final[tuple[str, ...]] = (
    "editorial_construction",
    "subtitle",
    "audio_finishing",
    "color_finishing",
    "framing_motion",
    "graphics_presentation",
    "delivery_qc",
)

FINISHING_RUN_RELATIVE: Final[tuple[str, ...]] = ("finishing", "finishing-run.json")

_StrTuple = Annotated[tuple[str, ...], BeforeValidator(to_tuple)]


class _FinishingRead(BaseModel):
    """Lenient projection: strict on the three display fields, ignore extras."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    episode_id: str = Field(min_length=1, strict=True)
    run_id: str = Field(min_length=1, strict=True)
    domain_statuses: dict[str, str]
    domain_justifications: dict[str, str]
    domain_proposed: dict[str, bool] = Field(default_factory=dict)
    domain_proposal_basis: dict[str, list[str]] = Field(default_factory=dict)
    blocked_domains: _StrTuple = ()


class FinishingStatusOps(WorkspaceContext):
    """Read-only finishing domain statuses for one episode."""

    def finishing_status(self, episode_id: str) -> dict[str, object]:
        snapshot = self._require_snapshot(episode_id)
        episode_dir = self._episode_dir(snapshot.job.episode_id)
        path = episode_dir.joinpath(*FINISHING_RUN_RELATIVE)
        if not path.is_file():
            return {"available": False, "domains": []}
        try:
            raw = path.read_bytes()
            payload = json.loads(raw)
            read = _FinishingRead.model_validate(payload)
        except (OSError, ValueError, ValidationError) as error:
            raise CockpitUnprocessableError(
                "finishing-run-invalid",
                f"{path} is not a valid finishing report: {error}",
            ) from error

        expected = set(QUALITY_DOMAIN_NAMES)
        if set(read.domain_statuses) != expected:
            raise CockpitUnprocessableError(
                "finishing-run-invalid",
                f"domain_statuses must carry exactly the seven quality domains; "
                f"got {sorted(read.domain_statuses)}",
            )

        blocked_set = set(read.blocked_domains)
        domains: list[dict[str, object]] = []
        for domain in QUALITY_DOMAIN_NAMES:
            status = read.domain_statuses[domain]
            justification = read.domain_justifications.get(domain)
            # ``applied`` domains must not carry a justification; writer enforces
            # it, but we surface whatever the file recorded (justification null
            # when absent so the UI can render absence honestly).
            domains.append(
                {
                    "domain": domain,
                    "status": status,
                    "justification": justification if justification is not None else None,
                    "blocked": domain in blocked_set,
                    "proposed": read.domain_proposed.get(domain, False),
                    "proposal_basis": read.domain_proposal_basis.get(domain, []),
                }
            )
        return {
            "available": True,
            "episode_id": read.episode_id,
            "run_id": read.run_id,
            "domains": domains,
        }


__all__ = ["FINISHING_RUN_RELATIVE", "QUALITY_DOMAIN_NAMES", "FinishingStatusOps"]
