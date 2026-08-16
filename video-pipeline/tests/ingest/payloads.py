"""Shared payload fragments for ingest analysis tests (model-level fault inputs).

Real DOVI RP-bearing containers cannot be produced by the pinned encoder
(h264_videotoolbox drops PQ/DOVI signaling; verified against ffprobe 7.1.1),
so HDR policy is exercised at the record/eligibility level with the payloads
below, per the Todo-21 brief ("model-level with documented reason").
"""

from __future__ import annotations

type PayloadObject = dict[str, object]

DOVI_VIDEO_STREAM: PayloadObject = {
    "index": 0,
    "codec_type": "video",
    "codec_name": "hevc",
    "time_base": "1/90000",
    "start_pts": 0,
    "duration_ts": 900000,
    "r_frame_rate": "24/1",
    "avg_frame_rate": "24/1",
    "width": 1920,
    "height": 1080,
    "pix_fmt": "yuv420p10le",
    "color_space": "bt2020nc",
    "color_transfer": "smpte2084",
    "color_primaries": "bt2020nc",
    "side_data_list": [
        {"side_data_type": "DOVI configuration record", "dv_profile": 8},
        {"side_data_type": "HDR Mastering Display Metadata Colour Primaries"},
        {"side_data_type": "Content Light Level Metadata"},
        {"side_data_type": "HDR Dynamic Metadata SMPTE2094-10"},
    ],
}

SMPTE2084_VIDEO_STREAM: PayloadObject = {
    "index": 0,
    "codec_type": "video",
    "codec_name": "h264",
    "time_base": "1/24000",
    "start_pts": 0,
    "duration_ts": 24000,
    "r_frame_rate": "24/1",
    "avg_frame_rate": "24/1",
    "width": 1920,
    "height": 1080,
    "pix_fmt": "yuv420p",
    "color_transfer": "smpte2084",
}

AUDIO_STREAM: PayloadObject = {
    "index": 1,
    "codec_type": "audio",
    "codec_name": "pcm_s16le",
    "time_base": "1/48000",
    "start_pts": 0,
    "duration_ts": 48000,
    "sample_rate": "48000",
    "channels": 1,
    "channel_layout": "mono",
}

FORMAT_PAYLOAD: PayloadObject = {
    "format": {
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "duration": "1.000000",
        "nb_streams": "2",
    }
}
