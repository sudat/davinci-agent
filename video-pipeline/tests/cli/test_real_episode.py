"""Todo 46 acceptance: the REAL-episode H1 path end to end.

A synthetic "real-format" episode (pinned TTS + pinned ffmpeg, NON-fixture
episode id) runs the real chain in deterministic-baseline mode to
PREVIEW_READY: bundle + preview + resolved policy snapshot. Free-form
corrections then flow through propose/apply exactly as the owner drives
them: a clear remove applies and regenerates, an ambiguous two-target
instruction never mutates, unknown targets are typed errors, and the LIVE
mode refuses honestly without the network flag (zero network in tests).
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from services.cli.bundle import load_bundle, rehash_bundle_targets
from services.cli.project import plan_sha256
from services.cli.real_chain import RealChainError, run_real_chain
from services.cli.real_director import director_mode
from services.cli.real_policy import load_policy
from services.cli.review_apply import apply_proposal
from services.cli.review_freeform import propose_freeform
from services.cli.review_replay import PlanView
from services.config.models import (
    BudgetPolicy,
    CloudAllowlistEntry,
    EpisodeConfig,
    NetworkPosture,
    PathAllowlist,
    ResolvedConfig,
    RetentionPolicy,
    StageDataClasses,
    SystemConfig,
)
from services.config.resolver import resolve
from services.editorial.transport import CREDENTIALS_ENV
from services.foundation_io import atomic_write, canonical_model_bytes
from services.review_command.store import load_head
from services.toolchain.models import Phase1TechnicalToolchainLock, load_lock

if sys.platform != "darwin":  # pragma: no cover - pinned TTS is macOS-only
    pytest.skip("real-episode synthesis (say/TTS) is macOS-only", allow_module_level=True)

EPISODE_ID = "real-h1-check-0001"
PHRASES = (
    "こんにちは。今日の撮影を始めます。",
    "まずはカメラの設定です。",
    "それでは、行きましょう。",
)
GAP_SECONDS = 2
LOCK = Path("config/toolchains/phase-1-technical-v2.json")


@dataclass(frozen=True, slots=True)
class RealRig:
    root: Path
    episode_root: Path
    out_dir: Path
    bundle_file: Path
    policy_file: Path


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(part) for part in argv],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, result.stderr[-1200:]
    return result


def _lock() -> Phase1TechnicalToolchainLock:
    lock = load_lock(LOCK)
    assert isinstance(lock, Phase1TechnicalToolchainLock)
    missing = [
        str(path)
        for path in (
            Path(lock.ffmpeg.ffmpeg.path),
            Path(lock.ffmpeg.ffprobe.path),
            Path(lock.whisper_ja.whisper_cli.path),
            Path(lock.whisper_ja.model.path),
            Path(lock.whisper_ja.tts.tool_path),
        )
        if not path.is_file()
    ]
    if missing:
        pytest.skip(f"pinned real-chain toolchain not bootstrapped: {missing}")
    return lock


def _build_episode(root: Path) -> Path:
    lock = _lock()
    ffmpeg = Path(lock.ffmpeg.ffmpeg.path)
    say = Path(lock.whisper_ja.tts.tool_path)
    voice = lock.whisper_ja.tts.voice
    episode_root = root / "episode"
    episode_root.mkdir(parents=True)
    clips: list[Path] = []
    for position, phrase in enumerate(PHRASES):
        aiff = root / f"tts-{position}.aiff"
        _run([str(say), "-v", voice, "-o", str(aiff), phrase], timeout=120)
        clips.append(aiff)
    parts = []
    for position, clip in enumerate(clips):
        parts.extend(("-i", str(clip)))
        if position + 1 < len(clips):
            silence = root / f"silence-{position}.wav"
            _run(
                [
                    str(ffmpeg),
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    f"anullsrc=r=44100:cl=mono:d={GAP_SECONDS}",
                    "-c:a",
                    "pcm_s16le",
                    "-y",
                    str(silence),
                ],
                timeout=120,
            )
            parts.extend(("-i", str(silence)))
    inputs = " ".join(f"[{index}:a]" for index in range(len(parts) // 2))
    speech = root / "speech.m4a"
    _run(
        [
            str(ffmpeg),
            "-v",
            "error",
            *parts,
            "-filter_complex",
            f"{inputs}concat=n={len(parts) // 2}:v=0:a=1",
            "-c:a",
            "aac",
            "-y",
            str(speech),
        ],
        timeout=180,
    )
    probe = json.loads(
        _run(
            [
                str(Path(lock.ffmpeg.ffprobe.path)),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(speech),
            ],
            timeout=120,
        ).stdout
    )
    seconds = math.ceil(float(probe["format"]["duration"]))
    original = episode_root / "original.mov"
    _run(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=640x360:rate=24:d={seconds}",
            "-i",
            str(speech),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "h264_videotoolbox",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(original),
        ],
        timeout=300,
    )
    (episode_root / "episode.json").write_text(
        json.dumps(
            {
                "schema_version": "real-episode-v1",
                "episode_id": EPISODE_ID,
                "video_path": "original.mov",
                "audio_path": None,
                "language": "ja",
                "declared_privacy_flags": [],
                "declared_rights_flags": [],
                "fixture_only": False,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return episode_root


@pytest.fixture(scope="session")
def rig(tmp_path_factory: pytest.TempPathFactory) -> RealRig:
    root = tmp_path_factory.mktemp("real-episode")
    episode_root = _build_episode(root)
    out_dir = root / "out"
    outcome = run_real_chain(episode_root, "PREVIEW_READY", out_dir, env={})
    assert outcome.bundle_file is not None
    bundle = load_bundle(outcome.bundle_file)
    assert bundle.fixture_only is False
    assert bundle.stage == "PREVIEW_READY"
    assert outcome.report.director_mode == "deterministic-baseline"
    return RealRig(
        root=root,
        episode_root=episode_root,
        out_dir=out_dir,
        bundle_file=outcome.bundle_file,
        policy_file=out_dir / "resolved-policy.json",
    )


def _plan_items(rig: RealRig) -> list[dict[str, object]]:
    bundle = load_bundle(rig.bundle_file)
    document = json.loads(
        (rig.bundle_file.parent / bundle.store_dir / "plan-v1.json").read_bytes()
    )
    items = document["plan"]["items"]
    assert isinstance(items, list)
    return [item for item in items if isinstance(item, dict)]


def _speech_ids(rig: RealRig) -> list[str]:
    return [
        str(item["item_id"])
        for item in _plan_items(rig)
        if item.get("kind") == "video"
    ]


def _subtitle_texts(rig: RealRig) -> dict[str, str]:
    return {
        str(item["item_id"]): str(item.get("subtitle_text"))
        for item in _plan_items(rig)
        if item.get("kind") == "subtitle"
    }


def _propose(rig: RealRig, instruction: str):
    bundle = load_bundle(rig.bundle_file)
    head = load_head(
        rig.bundle_file.parent / bundle.events_log,
        rig.bundle_file.parent / bundle.store_dir,
    )
    return propose_freeform(
        episode_id=bundle.episode_id,
        instruction=instruction,
        current=PlanView(
            plan=head.plan, version=f"v{head.version}", plan_hash=plan_sha256(head.plan)
        ),
        policy=load_policy(rig.policy_file),
        policy_sha="0" * 64,
        translator_sha="0" * 64,
    )


def test_00_chain_reaches_preview_ready_with_baseline_label(rig: RealRig) -> None:
    bundle = load_bundle(rig.bundle_file)
    rehash_bundle_targets(bundle, rig.bundle_file)
    assert bundle.eligibility_status == "supported"
    assert (rig.bundle_file.parent / "preview-v1" / "preview.mp4").is_file()
    assert rig.policy_file.is_file()
    report = json.loads((rig.out_dir / "run-report.json").read_bytes())
    assert report["schema_version"] == "real-chain-report-v1"
    assert report["director_mode"] == "deterministic-baseline"
    assert report["director_served_by"] == "deterministic-baseline-v1:no-model-involved"
    assert "deterministic-baseline-v1" in report["selection_producer"]
    assert director_mode({}) == "deterministic-baseline"
    versions = json.loads((rig.out_dir / "selection-plan" / "versions.json").read_bytes())
    assert versions["episode_id"] == EPISODE_ID


def test_05_plan_carries_speech_segments_and_subtitles(rig: RealRig) -> None:
    speech = _speech_ids(rig)
    assert len(speech) >= 2
    assert speech[0] == "s1"
    subtitles = _subtitle_texts(rig)
    assert len(subtitles) == len(speech)
    assert set(subtitles) == {f"st{index}" for index in range(1, len(speech) + 1)}


def test_10_freeform_remove_is_clear_and_applies(rig: RealRig) -> None:
    target = _speech_ids(rig)[-1]
    outcome = _propose(rig, f"セグメント {target} を削除してください。")
    assert outcome.status == "proposal"
    assert outcome.classification == "clear"
    assert tuple(outcome.candidate_item_ids) == (target,)
    proposal = rig.root / "prop-remove.json"
    atomic_write(proposal, canonical_model_bytes(outcome))
    result, code = apply_proposal(proposal, rig.bundle_file, rig.root / "apply-remove")
    assert code == 0
    assert result.applied is True
    assert result.version == 2
    bundle = load_bundle(rig.bundle_file)
    assert bundle.current.plan_version == "v2"
    assert (rig.bundle_file.parent / "preview-v2" / "preview.mp4").is_file()
    rehash_bundle_targets(bundle, rig.bundle_file)
    document = json.loads(
        (rig.bundle_file.parent / bundle.store_dir / "plan-v2.json").read_bytes()
    )
    kept = [
        item["item_id"]
        for item in document["plan"]["items"]
        if item.get("kind") == "video"
    ]
    assert target not in kept


def test_20_ambiguous_two_target_remove_never_mutates(rig: RealRig) -> None:
    speech = _speech_ids(rig)
    if len(speech) < 2:
        pytest.skip("ASR merged all speech into one segment")
    before = (rig.bundle_file.parent / "review-store" / "plan-v2.json").read_bytes()
    outcome = _propose(rig, f"セグメント {speech[0]} と {speech[1]} を削除してください。")
    assert outcome.status == "proposal"
    assert outcome.classification == "ambiguous"
    assert set(outcome.candidate_item_ids) == {speech[0], speech[1]}
    proposal = rig.root / "prop-amb.json"
    atomic_write(proposal, canonical_model_bytes(outcome))
    result, code = apply_proposal(proposal, rig.bundle_file, rig.root / "apply-amb")
    assert code == 0
    assert result.applied is False
    assert result.reason_code == "ambiguous"
    assert (
        rig.bundle_file.parent / "review-store" / "plan-v2.json"
    ).read_bytes() == before
    assert not (rig.bundle_file.parent / "preview-v3").exists()


def test_21_subtitle_text_correction_over_the_cli(rig: RealRig) -> None:
    document = json.loads(
        (rig.bundle_file.parent / "review-store" / "plan-v2.json").read_bytes()
    )
    subtitles = [
        (item["item_id"], item["subtitle_text"])
        for item in document["plan"]["items"]
        if item.get("kind") == "subtitle"
    ]
    texts = [text for _id, text in subtitles]
    unique = next(text for text in texts if texts.count(text) == 1)
    instruction_file = rig.root / "instr-sub.txt"
    instruction_file.write_text(
        f"「{unique}」という字幕を「修正された字幕です。」に修正してください。",
        encoding="utf-8",
    )
    proposal = rig.root / "prop-sub.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.cli.review",
            "propose",
            "--bundle",
            str(rig.bundle_file),
            "--instruction-file",
            str(instruction_file),
            "--translator-policy",
            "config/gates/phase-0c-v1.json",
            "--production-policy",
            str(rig.policy_file),
            "--out",
            str(proposal),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path.cwd(),
    )
    assert result.returncode == 0, result.stderr
    assert "classification: clear" in result.stdout
    applied = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.cli.review",
            "apply",
            "--proposal",
            str(proposal),
            "--bundle",
            str(rig.bundle_file),
            "--out",
            str(rig.root / "apply-sub"),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path.cwd(),
    )
    assert applied.returncode == 0, applied.stderr
    assert "applied: version v3" in applied.stdout
    updated = load_bundle(rig.bundle_file)
    assert updated.current.plan_version == "v3"
    assert (rig.bundle_file.parent / "preview-v3" / "preview.mp4").is_file()


def test_30_unknown_target_is_a_typed_error(rig: RealRig) -> None:
    outcome = _propose(rig, "セグメント s999 を削除してください。")
    assert outcome.status == "error"
    assert outcome.error_code == "unresolved_target"
    assert outcome.proposal_json is None


def test_31_unparsed_instruction_is_a_typed_error(rig: RealRig) -> None:
    outcome = _propose(rig, "全部いい感じにしてください")
    assert outcome.status == "error"
    assert outcome.error_code == "unparsed_instruction"


def _cloud_policy() -> ResolvedConfig:
    system = SystemConfig(
        schema_version="system-config-v1",
        retention=RetentionPolicy(authoritative="permanent", rebuildable_days=30),
        data_classes=(
            StageDataClasses(stage="review_translate", classes=("review_instruction_text",)),
            StageDataClasses(stage="editorial_direct", classes=("transcript",)),
        ),
        cloud_allowlist=(
            CloudAllowlistEntry(
                data_class="transcript", stage="editorial_direct", fixture_only=False
            ),
        ),
        network=NetworkPosture(
            builder="loopback", builder_endpoint="unix:///run/davinci-agent/editorial.sock"
        ),
        path_allowlist=PathAllowlist(roots=("/video-pipeline/jobs",)),
        budget=BudgetPolicy(
            transient_max_attempts=3,
            permanent_max_attempts=1,
            blocking_human_max_attempts=1,
            max_stage_cost_units=1000,
            max_job_cost_units=10000,
        ),
    )
    return resolve(system, episode=EpisodeConfig(episode_id=EPISODE_ID))


def test_40_live_transport_with_fake_key_and_no_network_flag_refuses_honestly() -> None:
    """FAKE credentials without the explicit network flag: honest refusal, no socket."""

    from services.cli.live_editorial import LiveHttpTransport  # noqa: PLC0415
    from services.editorial.pin import load_pin  # noqa: PLC0415
    from services.editorial.transport import (  # noqa: PLC0415
        EditorialTransportFailure,
    )

    transport = LiveHttpTransport(
        request=None,  # type: ignore[arg-type] (the gate refuses before the request)
        pin=load_pin(),
        env={CREDENTIALS_ENV: "value-never-sent"},
    )
    outcome = transport.send("a" * 64)
    assert isinstance(outcome, EditorialTransportFailure)
    assert outcome.code == "live_disabled"
    assert "EDITORIAL_DIRECTOR_NETWORK_ENABLED" in outcome.detail
    no_key = LiveHttpTransport(request=None, pin=load_pin(), env={}).send("a" * 64)  # type: ignore[arg-type]
    assert isinstance(no_key, EditorialTransportFailure)
    assert no_key.code == "no_credentials"


def test_41_live_mode_policy_is_denied_by_default() -> None:
    from services.cli.real_policy import local_only_policy  # noqa: PLC0415
    from services.editorial.policy import (  # noqa: PLC0415
        decide_production_transport_policy,
    )

    envelope = decide_production_transport_policy(
        EPISODE_ID, local_only_policy(EPISODE_ID)
    )
    assert envelope.allowed is False
    assert envelope.control_plane_decision == "deny"
    assert "local_only" in envelope.control_plane_reason
    granted = decide_production_transport_policy(EPISODE_ID, _cloud_policy())
    assert granted.allowed is True


def test_50_malformed_episode_json_is_a_typed_error(tmp_path: Path) -> None:
    root = tmp_path / "bad"
    root.mkdir()
    (root / "episode.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(RealChainError) as error:
        run_real_chain(root, "PREVIEW_READY", tmp_path / "out", env={})
    assert error.value.code == "episode_invalid"


def test_51_fixture_id_in_the_real_path_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "fixture-echo"
    root.mkdir()
    (root / "episode.json").write_text(
        json.dumps(
            {
                "schema_version": "real-episode-v1",
                "episode_id": "p1-ref-01-clean-ja",
                "video_path": "original.mov",
                "audio_path": None,
                "language": "ja",
                "declared_privacy_flags": [],
                "declared_rights_flags": [],
                "fixture_only": False,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RealChainError) as error:
        run_real_chain(root, "PREVIEW_READY", tmp_path / "out", env={})
    assert "fixture_id_refused" in str(error.value)


def test_52_phase1_cli_rejects_a_root_with_neither_manifest(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.cli.phase1",
            "run",
            "--episode-root",
            str(empty),
            "--stop",
            "PREVIEW_READY",
            "--out",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path.cwd(),
    )
    assert result.returncode == 1
    assert "episode_root_invalid" in result.stderr


def test_53_phase1_cli_serves_the_real_path(rig: RealRig, tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.cli.phase1",
            "run",
            "--episode-root",
            str(rig.episode_root),
            "--stop",
            "PREVIEW_READY",
            "--out",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path.cwd(),
        env={**os.environ, CREDENTIALS_ENV: ""},
        timeout=900,
    )
    assert result.returncode == 0, result.stderr
    assert f"episode: {EPISODE_ID}" in result.stdout
    assert "director mode: deterministic-baseline" in result.stdout
    assert (tmp_path / "out" / "review-bundle.json").is_file()


def test_60_fixture_manifests_still_route_to_the_fixture_chain(tmp_path: Path) -> None:
    """The five frozen fixtures keep their manifest.json dispatch (regression)."""

    source = Path("tests/fixtures/manifests/phase-1-technical")
    fixtures = sorted(source.glob("*.json"))
    assert len(fixtures) == 5
    episode_root = tmp_path / "fixture"
    episode_root.mkdir()
    shutil.copyfile(fixtures[0], episode_root / "manifest.json")
    from services.cli.chain import load_episode_manifest  # noqa: PLC0415

    manifest = load_episode_manifest(episode_root)
    assert manifest.fixture_only is True
