"""``python -m services.qc.run`` — the deterministic QC CLI.

``run`` executes every check over the bound inputs and writes the canonical
report (byte-identical for identical inputs). Exit codes: 0 passed, 1 any
blocker or unresolved human gate, 2 malformed input/usage. ``build-policy``
is the deterministic authoring step that derives a canonical resolved policy
from a clean fixture render (a proposal tool — the engine still verifies).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from services.foundation_io import atomic_write, canonical_model_bytes
from services.qc.checks import (
    check_audio,
    check_ir,
    check_preview,
    check_subtitles,
    check_video,
    committed_cues_from_ir,
)
from services.qc.checks.audio_checks import AudioMeasure, measure_audio
from services.qc.checks.video_checks import VideoCheckRequest
from services.qc.inputs import (
    LoadedInputs,
    OptionalBindings,
    QcInputError,
    capability_issues,
    load_inputs,
)
from services.qc.issue_factory import IssueFactory
from services.qc.models import (
    QC_ENGINE_VERSION,
    QcIssue,
    QcMeasured,
    QcPolicy,
    QcReport,
    QcToolVersions,
    UnresolvedHumanGate,
    compute_verdict,
)
from services.qc.policy_build import build_policy
from services.qc.privacy_gate import (
    evaluate_privacy_gate,
    guard_no_automated_privacy_detectors,
)
from services.qc.tools import QcToolError, QcTools, load_qc_tools, parse_probe_report
from services.qc.video_probe import PinnedBlackFreezeProbe

if TYPE_CHECKING:
    from services.contracts.timeline_ir import TimelineIrProduction

EXIT_PASSED: Final = 0
EXIT_BLOCKED: Final = 1
EXIT_MALFORMED: Final = 2


def _scope_guard_package() -> None:
    for source in sorted(Path(__file__).parent.rglob("*.py")):
        guard_no_automated_privacy_detectors(
            source.read_text(encoding="utf-8"), f"services/qc/{source.name}"
        )


def run_qc(
    render: Path,
    policy_path: Path,
    out: Path,
    extra: OptionalBindings | None = None,
) -> QcReport:
    """Execute every deterministic check; writes and returns the report."""

    _scope_guard_package()
    loaded = load_inputs(render, policy_path, extra or OptionalBindings())
    policy = loaded.policy
    tools = load_qc_tools()
    versions = QcToolVersions(
        qc_engine=QC_ENGINE_VERSION,
        ffmpeg_sha256=tools.ffmpeg_sha256,
        ffprobe_sha256=tools.ffprobe_sha256,
    )
    inputs = tuple(binding.sha256 for binding in loaded.bindings)
    factory = IssueFactory.for_policy(policy, inputs)
    issues: list[QcIssue] = []
    issues.extend(capability_issues(policy, inputs))
    issues.extend(check_ir(loaded.ir, policy, inputs))
    issues.extend(check_preview(loaded.preview, policy, inputs))
    try:
        report, _raw = parse_probe_report(tools.probe_raw(render))
    except QcToolError as error:
        issues.append(
            factory.build(
                "video_decode_failed",
                f"ffprobe cannot parse the render: {error}",
                tools.ffmpeg_sha256,
                (QcMeasured(name="probe", value="failed"),),
            )
        )
        return _finish(issues, (), loaded, versions, out)
    video = next((s for s in report.streams if s.codec_type == "video"), None)
    audio = next((s for s in report.streams if s.codec_type == "audio"), None)
    if video is None or audio is None:
        raise QcInputError("render carries no video/audio stream")
    probe = PinnedBlackFreezeProbe(tools)
    issues.extend(
        check_video(
            VideoCheckRequest(
                render=render,
                decode_outcome=tools.decode(render),
                report=report,
                policy=policy,
                inputs=inputs,
                ffmpeg_sha256=tools.ffmpeg_sha256,
                black_source=probe.black,
                freeze_source=probe.freeze,
            )
        )
    )
    issues.extend(_audio_issues(tools, render, audio.channels or 0, policy, inputs))
    has_subtitle_stream = any(
        stream.codec_type == "subtitle" for stream in report.streams
    )
    issues.extend(
        _subtitle_issues(
            tools, render, loaded.ir, policy, inputs, has_subtitle_stream=has_subtitle_stream
        )
    )
    privacy_issues, gates = evaluate_privacy_gate(
        loaded.declarations, policy.threshold_version, inputs
    )
    issues.extend(privacy_issues)
    return _finish(issues, gates, loaded, versions, out)


def _finish(
    issues: list[QcIssue],
    gates: tuple[UnresolvedHumanGate, ...],
    loaded: LoadedInputs,
    versions: QcToolVersions,
    out: Path,
) -> QcReport:
    issues.sort(key=lambda i: (i.rule_id, i.detail))
    typed_gates = tuple(sorted(gates, key=lambda g: g.gate_id))
    report_out = QcReport(
        schema_version="qc-report-v1",
        verdict=compute_verdict(tuple(issues), typed_gates),
        issues=tuple(issues),
        unresolved_human_gates=typed_gates,
        tool_versions=versions,
        threshold_version=loaded.policy.threshold_version,
        inputs=loaded.bindings,
    )
    atomic_write(out, canonical_model_bytes(report_out))
    return report_out


def _audio_issues(
    tools: QcTools, render: Path, channels: int, policy: QcPolicy, inputs: tuple[str, ...]
) -> tuple[QcIssue, ...]:
    with tempfile.TemporaryDirectory(prefix="qc-audio-") as tmp_dir:
        wav = Path(tmp_dir) / "measure.wav"
        measured = measure_audio(tools.ffmpeg, render, tools.mono_wav, wav)
    measure = AudioMeasure(
        peak_sample=measured.peak_sample,
        peak_mb=measured.peak_mb,
        clipped_samples=measured.clipped_samples,
        loudness=measured.loudness,
        silence_spans=measured.silence_spans,
        probed_channels=channels,
    )
    return check_audio(measure, policy, inputs)


def _subtitle_issues(  # noqa: PLR0913 (bundled subtitle pass inputs)
    tools: QcTools,
    render: Path,
    ir: TimelineIrProduction | None,
    policy: QcPolicy,
    inputs: tuple[str, ...],
    *,
    has_subtitle_stream: bool,
) -> tuple[QcIssue, ...]:
    subtitle_srt = tools.demux_subtitle(render) if has_subtitle_stream else None
    return check_subtitles(subtitle_srt, committed_cues_from_ir(ir), policy, inputs)


def _normalize(argv: list[str] | None) -> list[str]:
    """The plan's acceptance invokes the CLI without a subcommand; ``run``
    is the default."""

    resolved = list(argv) if argv is not None else sys.argv[1:]
    if resolved and resolved[0].startswith("-"):
        return ["run", *resolved]
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="services.qc.run")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--render", type=Path, required=True)
    run_parser.add_argument("--policy", type=Path, required=True)
    run_parser.add_argument("--out", type=Path, required=True)
    run_parser.add_argument("--ir", type=Path)
    run_parser.add_argument("--preview", type=Path)
    run_parser.add_argument("--analysis", type=Path)
    run_parser.add_argument("--privacy", type=Path)
    build_parser = sub.add_parser("build-policy")
    build_parser.add_argument("--render", type=Path, required=True)
    build_parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args(_normalize(argv))
    try:
        if arguments.command == "build-policy":
            policy = build_policy(arguments.render)
            atomic_write(arguments.out, canonical_model_bytes(policy))
            print(f"policy written: {arguments.out} version={policy.threshold_version}")
            return EXIT_PASSED
        report = run_qc(
            arguments.render,
            arguments.policy,
            arguments.out,
            OptionalBindings(
                ir=arguments.ir,
                preview=arguments.preview,
                analysis=arguments.analysis,
                privacy=arguments.privacy,
            ),
        )
    except (QcInputError, QcToolError, ValidationError, OSError) as error:
        print(f"qc input error: {error}", file=sys.stderr)
        return EXIT_MALFORMED
    blockers = sum(1 for issue in report.issues if issue.severity == "blocker")
    print(
        f"qc verdict: {report.verdict} issues={len(report.issues)} "
        f"blockers={blockers} gates={len(report.unresolved_human_gates)}"
    )
    return EXIT_PASSED if report.verdict == "passed" else EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
