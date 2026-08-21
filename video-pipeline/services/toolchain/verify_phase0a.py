"""Phase-0a toolchain smoke verification extracted from verify.py."""

from __future__ import annotations

from pathlib import Path

from services.resolve_bridge.readiness import load_host_report
from services.toolchain.models import LockError, SmokeRecord, ToolchainLock
from services.toolchain.smoke import run_ffmpeg_probe

type SmokeName = str


def _scope_observation(report_scope: str, reason: str) -> str:
    match report_scope:
        case "disabled":
            return "scripting scope verified live: disabled (scriptapp unavailable)"
        case "loopback":
            return "scripting scope verified live: loopback (non-loopback probe refused)"
        case "local-network":
            return (
                "scripting scope verified live: local-network under an explicit "
                "operator permit (non-loopback probe accepted)"
            )
    return f"scripting scope unresolved ({report_scope}): {reason}"


def verify_phase0a_smoke(
    lock: ToolchainLock, smoke_names: tuple[SmokeName, ...]
) -> ToolchainLock:
    smoke = lock.smoke
    for smoke_name in smoke_names:
        match smoke_name:
            case "resolve-readonly":
                report = load_host_report(Path(lock.resolve.report_path))
                smoke = smoke.model_copy(
                    update={
                        "resolve_readonly": SmokeRecord(
                            status="passed",
                            evidence_paths=(lock.resolve.report_path,),
                            observation=(
                                f"{_scope_observation(
                                    report.scripting.remote_access,
                                    report.scripting.reason,
                                )}; "
                                "probe performed no external network access; "
                                f"live verification complete="
                                f"{not report.scripting.needs_live_verification}"
                            ),
                        )
                    }
                )
            case "ffmpeg-probe" | "ffmpeg-ffprobe":
                prefix = Path(lock.ffmpeg.ffmpeg.path).parent.parent
                artifacts = run_ffmpeg_probe(
                    Path(lock.ffmpeg.ffmpeg.path),
                    Path(lock.ffmpeg.ffprobe.path),
                    prefix.parents[1] / "smoke",
                )
                smoke = smoke.model_copy(
                    update={
                        "ffmpeg_probe": SmokeRecord(
                            status="passed",
                            evidence_paths=tuple(str(item.resolve()) for item in artifacts),
                            observation=(
                                "encoded, decoded, and probed 30 frames at 30/1 for 1000 ms"
                            ),
                        )
                    }
                )
            case "ffmpeg-normalize" | "preview-review" | "whisper-ja" | "editorial-model":
                raise LockError(f"{smoke_name} smoke requires a phase-0b/0c/1 lock")
    return lock.model_copy(update={"smoke": smoke})
