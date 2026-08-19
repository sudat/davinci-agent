from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

import pytest

from services.ingest.ingest import recipe_pointer, register_one
from services.ingest.probe import probe_media
from tests.ingest.conftest import PIN_PATH


def test_register_one_ignores_attached_pic_stream(
    monkeypatch: pytest.MonkeyPatch,
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
) -> None:
    media = tmp_path / "attached-pic.mp4"
    subprocess.run(
        [
            str(pinned_ffmpeg),
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=24:duration=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:size=64x64:rate=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-map",
            "0:v:0",
            "-map",
            "1:v:0",
            "-map",
            "2:a:0",
            "-t",
            "1",
            "-c:v",
            "h264_videotoolbox",
            "-c:a",
            "aac",
            str(media),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = probe_media(pinned_ffprobe, media)
    streams = cast("list[dict[str, object]]", payload["streams"])
    streams[1]["disposition"] = {"attached_pic": 1}
    streams[1]["avg_frame_rate"] = "0/0"
    monkeypatch.setattr("services.ingest.ingest.probe_media", lambda *_args: payload)

    manifest = register_one(
        original=media,
        ffprobe=pinned_ffprobe,
        recipe=recipe_pointer(PIN_PATH, "p0b-cfr24"),
        out=tmp_path / "source-manifest.json",
    )

    assert [stream.codec_type for stream in manifest.streams] == ["video", "audio"]
    assert manifest.eligibility.verdict == "supported"
