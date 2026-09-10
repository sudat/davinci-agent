"""Dual-route selection, AV consent gate, Gemini validation (PRD v4.4 §8.5).

Pure-function and tmp-dir tests only — no network, no GLM, no Gemini.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.media_intelligence.gemini_av_observation import (
    av_call_key,
    cost_from_usage,
    load_cached_av_call,
    store_av_call,
    validate_av_events,
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


def test_validation_keeps_in_bounds_events_clean() -> None:
    validation = validate_av_events(
        [_event(0.0, 56.0, "導入"), _event(56.0, 101.0)], 282.24
    )
    assert validation.route_quality_insufficient is False
    assert validation.drift_flags == ()
    assert [e.start_seconds for e in validation.events] == [0.0, 56.0]


def test_validation_clamps_tail_drift_and_flags_it() -> None:
    # The live A/B shape: 2/5 events drift past the 282.24s duration.
    validation = validate_av_events(
        [
            _event(0.0, 56.0, "導入"),
            _event(56.0, 101.0),
            _event(101.0, 213.0),
            _event(213.0, 332.0, "混乱"),
            _event(332.0, 441.0, "独白"),
        ],
        282.24,
    )
    assert [e.end_seconds for e in validation.events][3:] == [282.24, 282.24]
    assert [(f.index, f.raw_end) for f in validation.drift_flags] == [
        (3, 332.0),
        (4, 441.0),
    ]
    # 2/5 = 40% drift exceeds the 30% bar → insufficient, never fatal.
    assert validation.route_quality_insufficient is True


def test_validation_empty_or_malformed_is_typed() -> None:
    assert validate_av_events([], 100.0).route_quality_insufficient is True
    with pytest.raises(SampleObservationError) as exc_info:
        validate_av_events([{"start_seconds": "soon"}], 100.0)
    assert exc_info.value.code == "sample-av-bad-response"


def test_cost_split_matches_live_ab_numbers() -> None:
    # gemini-run.json usage: TEXT 304 + VIDEO 25662 prompt, 1160 out.
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
    assert cost == {"input_usd": 0.007729, "output_usd": 0.000464, "total_usd": 0.008193}


def test_call_cache_reuses_same_contract_only(tmp_path: Path) -> None:
    key = av_call_key(
        file_sha256="abc", model_id="m", prompt_sha256="p", schema_sha256="s"
    )
    assert load_cached_av_call(tmp_path, key) is None
    record = {"model": "m", "events": []}
    store_av_call(tmp_path, key, record)
    assert load_cached_av_call(tmp_path, key) == record
    other = av_call_key(
        file_sha256="different", model_id="m", prompt_sha256="p", schema_sha256="s"
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
