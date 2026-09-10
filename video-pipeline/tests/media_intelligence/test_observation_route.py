"""Dual-route selection, AV consent gate, Gemini validation (PRD v4.4 §8.5).

Pure-function and tmp-dir tests only — no network, no GLM, no Gemini.
"""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

import pytest

from services.media_intelligence.gemini_av_models import (
    adopt_core_events,
    partition_av_cores,
    validate_chunk_events,
)
from services.media_intelligence.gemini_av_record import (
    chunk_av_call_key,
    cost_from_usage,
    load_cached_av_call,
    store_av_call,
)
from services.media_intelligence.observation_route import (
    AvAnalysisConsentV1,
    read_av_consent,
    select_observation_route,
)
from services.media_intelligence.sample_observation import SampleObservationError


def test_explicit_override_always_wins() -> None:
    assert (
        select_observation_route(
            override="audiovisual", has_transcript=False, policy_text=""
        )
        == "audiovisual"
    )
    assert (
        select_observation_route(
            override="visual",
            has_transcript=True,
            policy_text="BGMの音を重視する",
        )
        == "visual"
    )


def test_auto_picks_audiovisual_only_with_transcript_and_audio_policy() -> None:
    assert (
        select_observation_route(
            override="auto",
            has_transcript=True,
            policy_text="語りの声とBGMのバランスを取る",
        )
        == "audiovisual"
    )
    assert (
        select_observation_route(
            override=None, has_transcript=True, policy_text="narration timing matters"
        )
        == "audiovisual"
    )


def test_auto_stays_visual_without_transcript() -> None:
    assert (
        select_observation_route(
            override="auto",
            has_transcript=False,
            policy_text="音楽と声を重視する",
        )
        == "visual"
    )


def test_auto_stays_visual_without_audio_policy() -> None:
    assert (
        select_observation_route(
            override="auto",
            has_transcript=True,
            policy_text="明るい色合いでテンポよく",
        )
        == "visual"
    )


def test_unspecified_audio_policy_boilerplate_stays_visual() -> None:
    # Live-measured 2026-09-11: every policy dump carries an audio_policy
    # field whose "未確認…指定されていない。" boilerplate contains 音/音楽 —
    # a bare substring match routed nearly every episode to audiovisual.
    policy = json.dumps(
        {
            "scope": {"composition": True, "appearance": True, "audio": False},
            "audio_policy": "未確認。会話、現場音、音楽の優先順位は指定されていない。",
            "tempo_policy": "動きのある場面を中心に",
        },
        ensure_ascii=False,
    )
    assert (
        select_observation_route(
            override="auto", has_transcript=True, policy_text=policy
        )
        == "visual"
    )


def test_positive_audio_policy_sentence_routes_audiovisual() -> None:
    policy = json.dumps(
        {
            "scope": {"composition": True, "appearance": True, "audio": False},
            "audio_policy": "BGMを従来より前に出します。声や現場音との音量バランスは未確認です。",
        },
        ensure_ascii=False,
    )
    assert (
        select_observation_route(
            override="auto", has_transcript=True, policy_text=policy
        )
        == "audiovisual"
    )


def test_scope_audio_switch_routes_audiovisual() -> None:
    policy = json.dumps(
        {"scope": {"audio": True}, "audio_policy": "未確認。"},
        ensure_ascii=False,
    )
    assert (
        select_observation_route(
            override="auto", has_transcript=True, policy_text=policy
        )
        == "audiovisual"
    )


def _write_consent(
    episode_dir: Path, episode_id: str, *, allowed: bool, note: str = ""
) -> Path:
    path = episode_dir / "av-analysis-consent.json"
    path.write_text(
        AvAnalysisConsentV1(
            episode_id=episode_id,
            cloud_media_av_analysis=allowed,
            note=note,
        ).model_dump_json(),
        encoding="utf-8",
    )
    return path


def test_consent_missing_file_is_no_consent(tmp_path: Path) -> None:
    assert read_av_consent(tmp_path, "ep-1") is False


def test_consent_explicit_true_allows(tmp_path: Path) -> None:
    _write_consent(tmp_path, "ep-1", allowed=True)
    assert read_av_consent(tmp_path, "ep-1") is True


def test_consent_false_or_mismatch_or_malformed_denies(tmp_path: Path) -> None:
    _write_consent(tmp_path, "ep-1", allowed=False)
    assert read_av_consent(tmp_path, "ep-1") is False
    _write_consent(tmp_path, "ep-other", allowed=True)
    assert read_av_consent(tmp_path, "ep-1") is False
    (tmp_path / "av-analysis-consent.json").write_text("{not json", encoding="utf-8")
    assert read_av_consent(tmp_path, "ep-1") is False


def _event(start: float, end: float, reason: str = "") -> dict[str, object]:
    return {
        "start_seconds": start,
        "end_seconds": end,
        "visual": "見えている内容",
        "speech": "発話",
        "ambient": "",
        "music": "",
        "audiovisual_relation": "同期",
        "sample_candidate_reason": reason,
    }


def test_cores_cover_duration_without_overlap_or_gap() -> None:
    # Reviewer P1-1 (1): property-style over several durations incl. 282.24s.
    for duration in (30.0, 60.0, 60.5, 120.0, 282.24, 300.0):
        chunks = partition_av_cores(duration)
        assert chunks[0].core_start == 0.0
        assert chunks[-1].core_end == duration
        for previous, current in pairwise(chunks):
            assert current.core_start == previous.core_end
            assert current.index == previous.index + 1
        for chunk in chunks:
            assert chunk.clip_start == max(0.0, chunk.core_start - 3.0)
            assert chunk.clip_end == min(duration, chunk.core_end + 3.0)
            assert chunk.clip_duration_seconds == chunk.clip_end - chunk.clip_start
    five = partition_av_cores(282.24)
    assert len(five) == 5
    assert [(c.core_start, c.core_end) for c in five] == [
        (0.0, 60.0),
        (60.0, 120.0),
        (120.0, 180.0),
        (180.0, 240.0),
        (240.0, 282.24),
    ]
    assert (five[0].clip_start, five[0].clip_end) == (0.0, 63.0)
    assert (five[-1].clip_start, five[-1].clip_end) == (237.0, 282.24)


def test_context_region_starts_are_not_adopted() -> None:
    # Reviewer P1-1 (2): a 61.0s-global start lives in chunk 1's core, so
    # chunk 0 (clip [0, 63)) drops it while chunk 1 adopts it — no dup.
    chunks = partition_av_cores(282.24)
    first, second = chunks[0], chunks[1]
    validation = validate_chunk_events(
        [_event(10.0, 20.0, "導入"), _event(61.0, 62.0, "文脈")], first
    )
    assert validation.chunk_insufficient is False
    assert [e.start_seconds for e in adopt_core_events(validation, first)] == [10.0]
    neighbor = validate_chunk_events([_event(4.0, 5.0, "文脈")], second)
    adopted = adopt_core_events(neighbor, second)
    assert [e.start_seconds for e in adopted] == [61.0]


def test_out_of_range_events_never_become_candidates() -> None:
    # Reviewer P1-1 (3): the live failure shape (chunk-0 clip is 63s; a
    # 30-70s local event exceeds it) marks the chunk insufficient and
    # adopts nothing — no clamp, no rescue.
    chunk = partition_av_cores(282.24)[0]
    validation = validate_chunk_events([_event(10.0, 20.0), _event(30.0, 70.0)], chunk)
    assert validation.chunk_insufficient is True
    assert validation.events == ()
    assert [(f.index, f.raw_end) for f in validation.drift_flags] == [(1, 70.0)]
    assert adopt_core_events(validation, chunk) == ()


def test_validation_keeps_in_bounds_events_clean() -> None:
    chunk = partition_av_cores(282.24)[0]
    validation = validate_chunk_events(
        [_event(0.0, 56.0, "導入"), _event(56.0, 62.0)], chunk
    )
    assert validation.chunk_insufficient is False
    assert validation.drift_flags == ()
    assert [e.start_seconds for e in validation.events] == [0.0, 56.0]


def test_end_at_clip_duration_is_valid() -> None:
    # Live regression (2026-09-11): 4/5 chunks were discarded solely
    # because their LAST event ended exactly AT the clip duration (63.0
    # on a 63s clip, 66.0 on a 66s clip) — the natural closing boundary.
    for chunk in partition_av_cores(282.24)[:4]:
        last = _event(50.0, chunk.clip_duration_seconds, "結び")
        local = validate_chunk_events(
            [_event(0.5, 10.0, "導入"), last], chunk
        )
        assert local.chunk_insufficient is False, chunk.index
        assert local.drift_flags == ()
        assert len(local.events) == 2


def test_validation_empty_or_malformed_is_typed() -> None:
    chunk = partition_av_cores(100.0)[0]
    assert validate_chunk_events([], chunk).chunk_insufficient is True
    with pytest.raises(SampleObservationError) as exc_info:
        validate_chunk_events([{"start_seconds": "soon"}], chunk)
    assert exc_info.value.code == "sample-av-bad-response"


def test_cost_split_matches_live_ab_numbers() -> None:
    # gemini-run.json usage: TEXT 304 + VIDEO 25662 prompt, 1160 out —
    # at the corrected gemini-3.5-flash-lite Standard rates (all input
    # $0.30/1M, output $2.50/1M).
    cost, method = cost_from_usage(
        {
            "promptTokenCount": 25966,
            "candidatesTokenCount": 1160,
            "promptTokensDetails": [
                {"modality": "TEXT", "tokenCount": 304},
                {"modality": "VIDEO", "tokenCount": 25662},
            ],
        }
    )
    assert method == "per-modality promptTokensDetails"
    assert cost == {"input_usd": 0.00779, "output_usd": 0.0029, "total_usd": 0.01069}


def test_cost_math_with_corrected_35_rates() -> None:
    # Reviewer P1-2 (4): C4 call = 25,974 input + 1,182 output.
    cost, _ = cost_from_usage(
        {
            "promptTokenCount": 25974,
            "candidatesTokenCount": 1182,
            "promptTokensDetails": [
                {"modality": "TEXT", "tokenCount": 312},
                {"modality": "VIDEO", "tokenCount": 25662},
            ],
        }
    )
    assert cost == {"input_usd": 0.007792, "output_usd": 0.002955, "total_usd": 0.010747}


def test_call_cache_reuses_same_contract_only(tmp_path: Path) -> None:
    key = chunk_av_call_key(
        proxy_sha256="abc",
        chunk_index=0,
        core_start_seconds=0.0,
        core_end_seconds=60.0,
        model_id="m",
        prompt_sha256="p",
        schema_sha256="s",
    )
    assert load_cached_av_call(tmp_path, key) is None
    record = {"model": "m", "events": []}
    store_av_call(tmp_path, key, record)
    assert load_cached_av_call(tmp_path, key) == record
    other = chunk_av_call_key(
        proxy_sha256="different",
        chunk_index=0,
        core_start_seconds=0.0,
        core_end_seconds=60.0,
        model_id="m",
        prompt_sha256="p",
        schema_sha256="s",
    )
    assert load_cached_av_call(tmp_path, other) is None


def test_consent_model_never_records_a_key(tmp_path: Path) -> None:
    path = _write_consent(tmp_path, "ep-1", allowed=True, note="operator ok")
    dumped = json.loads(path.read_text(encoding="utf-8"))
    assert "GEMINI_API_KEY" not in json.dumps(dumped)
    assert set(dumped) <= {
        "schema_version",
        "episode_id",
        "cloud_media_av_analysis",
        "granted_at",
        "note",
    }
