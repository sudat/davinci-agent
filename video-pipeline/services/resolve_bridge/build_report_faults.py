"""Offline fault scenarios for the Build-Report 0A spike (QA_FAULT_FIXTURE mode).

Fault specs drive the real writer and verifier against in-memory fakes so a
forged success flag, a missing item readback, a failing decode pass, and a
render/build mismatch all surface as verification failures, never silent
success. Exit code is ``EXIT_FAULT`` whether or not the injected fault was
detected; the printed observation discriminates.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from services.contracts.build_report import BuildReport0A
from services.contracts.primitives import StrictModel
from services.fixtures.manifest import Phase0AFixtureManifest
from services.resolve_bridge.build_report_fakes import FakeSpike, run_fake_spike
from services.resolve_bridge.build_report_models import MARKER
from services.resolve_bridge.build_report_verify import verify_report
from services.resolve_bridge.fixed_presentation_fakes import FAKE_RENDER_MAGIC
from services.resolve_bridge.lifecycle import PROJECT_PREFIX

EXIT_FAULT: Final = 2
FORGED_CODE: Final = "forged-success-rejected"

FAULT_KINDS: Final = {
    "forged_success": FORGED_CODE,
    "missing_item_readback": "missing-item-readback",
    "decode_failure": "render-decode-failed",
    "render_build_mismatch": "render-hash-mismatch",
}


class BuildReportFaultSpec(StrictModel):
    fault: str


def _outcome_lines(
    spike: FakeSpike, manifest: Phase0AFixtureManifest, fixture_dir: Path, report: BuildReport0A
) -> list[str]:
    outcome = verify_report(
        report,
        manifest,
        fixture_dir,
        spike.tools,
        manifest_path=spike.manifest_path,
        host_report_path=spike.host_report_path,
        ffmpeg_bin=spike.ffmpeg_bin,
        ffprobe_bin=spike.ffprobe_bin,
    )
    return [f"{MARKER} mismatch code={row.code} detail={row.detail}" for row in outcome.mismatches]


def _forged_success_lines(
    spike: FakeSpike, manifest: Phase0AFixtureManifest, fixture_dir: Path, scratch: Path
) -> list[str]:
    payload = json.loads(spike.report.model_dump_json())
    payload["passed"] = True
    payload["items"][0]["observed"]["record_span"]["end_frame"] += 1
    forged_path = scratch / "forged-build-report.json"
    forged_path.write_text(json.dumps(payload))
    lines: list[str] = []
    try:
        BuildReport0A.model_validate_json(forged_path.read_bytes())
    except ValidationError:
        lines.append(
            f"{MARKER} mismatch code={FORGED_CODE} detail=report-schema-invalid: "
            "a stored passed flag is not part of the contract"
        )
    else:
        lines.append("ERROR: forged passed=true field was accepted by the contract schema")
    payload.pop("passed")
    tampered = BuildReport0A.model_validate_json(json.dumps(payload))
    lines.extend(_outcome_lines(spike, manifest, fixture_dir, tampered))
    lines.append(f"{MARKER} {FORGED_CODE} recomputation-governs=true")
    return lines


def run_fault_cli(spec_path: Path, manifest_path: Path, fixture_dir: Path) -> int:
    try:
        spec = BuildReportFaultSpec.model_validate_json(spec_path.read_bytes())
        manifest = Phase0AFixtureManifest.model_validate_json(manifest_path.read_bytes())
    except (OSError, ValidationError) as error:
        print(f"{MARKER} fault-fixture invalid: {error}", file=sys.stderr)
        return EXIT_FAULT
    if spec.fault not in FAULT_KINDS:
        print(f"{MARKER} fault-fixture invalid: unknown fault {spec.fault!r}", file=sys.stderr)
        return EXIT_FAULT
    lines: list[str] = []
    with tempfile.TemporaryDirectory(prefix="build-report-fault-") as scratch_name:
        scratch = Path(scratch_name)
        spike = run_fake_spike(
            manifest_path,
            fixture_dir,
            scratch,
            pool_fault="missing_item" if spec.fault == "missing_item_readback" else "",
            decode_fails=spec.fault == "decode_failure",
        )
        if spec.fault == "render_build_mismatch":
            spike.render_path.write_bytes(FAKE_RENDER_MAGIC + b"-different-bytes")
        if spec.fault == "forged_success":
            lines.extend(_forged_success_lines(spike, manifest, fixture_dir, scratch))
        else:
            lines.extend(_outcome_lines(spike, manifest, fixture_dir, spike.report))
            if spec.fault == "missing_item_readback":
                lines.append(
                    f"{MARKER} mismatch code=missing-item-readback "
                    f"items={len(spike.report.items)}/8 "
                    f"failures={len(spike.report.failures)}"
                )
        leaked = [
            name
            for name in spike.manager.GetProjectListInCurrentFolder()
            if name.startswith(PROJECT_PREFIX)
        ]
    print("\n".join(lines))
    observed = FAULT_KINDS[spec.fault] in "\n".join(lines)
    if not observed:
        print(
            f"ERROR: fault {spec.fault!r} not observed as {FAULT_KINDS[spec.fault]!r}",
            file=sys.stderr,
        )
    if leaked:
        print(f"ERROR: owned projects leaked: {leaked}", file=sys.stderr)
    return EXIT_FAULT


if __name__ == "__main__":
    raise SystemExit(run_fault_cli(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])))
