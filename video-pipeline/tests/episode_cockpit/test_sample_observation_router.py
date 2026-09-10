"""Routed observation + real-index transcript read (PRD v4.4 §8.5).

Hermetic: duckdb fixtures in tmp dirs, monkeypatched observation seams —
no network, no GLM, no Gemini.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

import duckdb
import pytest

from services.contracts.primitives import RecordFrameSpan
from services.episode_cockpit import sample_observation_router as router
from services.episode_cockpit.sample_observation_server import (
    _real_index_segments,
    _transcript_segments,
    episode_has_transcript,
)
from services.media_intelligence.observation_route import AvAnalysisConsentV1
from services.media_intelligence.sample_observation import SampleObservationError
from tests.compile.test_sample_projection import _full_ir


class _PolicyStub:
    """Minimal adopted-policy stand-in (only model_dump_json is read)."""

    def __init__(self, text: str) -> None:
        self._text = text

    def model_dump_json(self) -> str:
        return json.dumps({"audio": self._text}, ensure_ascii=False)


def _write_real_index(episode_dir: Path, rows: list[tuple[str, int, int, str]]) -> Path:
    path = episode_dir / "run" / "episode" / "media.duckdb"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE transcript_segments ("
            "source_id TEXT NOT NULL, segment_index INTEGER NOT NULL, "
            "start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, text TEXT NOT NULL, "
            "confidence INTEGER, analyzer_version TEXT NOT NULL, "
            "artifact_sha TEXT NOT NULL)"
        )
        for index, (source, start_ms, end_ms, text) in enumerate(rows):
            connection.execute(
                "INSERT INTO transcript_segments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [source, index, start_ms, end_ms, text, 90, "todo33-v1", "sha" + str(index)],
            )
    finally:
        connection.close()
    return path


def _write_consent(episode_dir: Path, episode_id: str) -> None:
    (episode_dir / "av-analysis-consent.json").write_text(
        AvAnalysisConsentV1(
            episode_id=episode_id,
            cloud_media_av_analysis=True,
        ).model_dump_json(),
        encoding="utf-8",
    )


def test_real_index_segments_read_seconds(tmp_path: Path) -> None:
    _write_real_index(
        tmp_path,
        [("src", 0, 4820, "どこ行ったかな"), ("src", 94000, 97900, "三脚とかも")],
    )
    assert _real_index_segments(tmp_path) == [
        (0.0, 4.82, "どこ行ったかな"),
        (94.0, 97.9, "三脚とかも"),
    ]
    assert episode_has_transcript(tmp_path) is True


def test_real_index_absent_falls_back_to_typed_missing(tmp_path: Path) -> None:
    assert _real_index_segments(tmp_path) is None
    assert episode_has_transcript(tmp_path) is False
    with pytest.raises(SampleObservationError) as exc_info:
        _transcript_segments(tmp_path, 100, Fraction(30, 1))
    assert exc_info.value.code == "sample-observation-unavailable"


def test_transcript_segments_prefers_real_index(tmp_path: Path) -> None:
    _write_real_index(tmp_path, [("src", 1000, 2000, "real-words")])
    assert _transcript_segments(tmp_path, 100, Fraction(30, 1)) == [
        (1.0, 2.0, "real-words")
    ]


def test_explicit_audiovisual_without_consent_is_typed_blocked(
    tmp_path: Path,
) -> None:
    with pytest.raises(SampleObservationError) as exc_info:
        router.resolve_routed_sample_windows(
            _full_ir(), tmp_path, "ep-1", route_override="audiovisual"
        )
    assert exc_info.value.code == "sample-av-consent-missing"


def test_explicit_visual_never_touches_av(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_av(full_ir: Any, root: Path, eid: str) -> tuple[list[Any], str]:
        calls.append("av")
        raise AssertionError("AV must not run on the visual route")

    def fake_visual(
        full_ir: Any, root: Path, eid: str
    ) -> tuple[list[RecordFrameSpan], str]:
        return (
            [RecordFrameSpan(start_frame=0, end_frame=30)],
            json.dumps({"anchors_seconds": [1.0]}),
        )

    monkeypatch.setattr(router, "resolve_av_sample_windows", fake_av)
    monkeypatch.setattr(router, "resolve_server_sample_windows", fake_visual)
    windows, observation = router.resolve_routed_sample_windows(
        _full_ir(), tmp_path, "ep-1", route_override="visual"
    )
    assert calls == []
    assert windows == [RecordFrameSpan(start_frame=0, end_frame=30)]
    assert json.loads(observation)["route"] == "visual"


def test_auto_av_failure_falls_back_visual_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_real_index(tmp_path, [("src", 0, 4000, "話し言葉がある")])
    _write_consent(tmp_path, "ep-1")
    monkeypatch.setattr(router, "episode_has_transcript", lambda root: True)
    monkeypatch.setattr(
        router,
        "latest_adopted_policy",
        lambda root: _PolicyStub("語りの声とBGMのバランスを取る"),
    )

    def fake_av(full_ir: Any, root: Path, eid: str) -> tuple[list[Any], str]:
        raise SampleObservationError("sample-av-provider-error", "Gemini HTTP 500")

    def fake_visual(
        full_ir: Any, root: Path, eid: str
    ) -> tuple[list[RecordFrameSpan], str]:
        return (
            [RecordFrameSpan(start_frame=0, end_frame=30)],
            json.dumps({"anchors_seconds": [1.0]}),
        )

    monkeypatch.setattr(router, "resolve_av_sample_windows", fake_av)
    monkeypatch.setattr(router, "resolve_server_sample_windows", fake_visual)
    windows, observation = router.resolve_routed_sample_windows(
        _full_ir(), tmp_path, "ep-1", route_override="auto"
    )
    assert windows == [RecordFrameSpan(start_frame=0, end_frame=30)]
    record = json.loads(observation)
    assert record["route"] == "visual"
    assert record["audio_integration"] == "unconfirmed"
    assert "sample-av-provider-error" in record["route_fallback_note"]


def test_explicit_audiovisual_failure_is_not_substituted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_consent(tmp_path, "ep-1")
    monkeypatch.setattr(router, "episode_has_transcript", lambda root: True)
    monkeypatch.setattr(
        router, "latest_adopted_policy", lambda root: _PolicyStub("BGMあり")
    )

    def fake_av(full_ir: Any, root: Path, eid: str) -> tuple[list[Any], str]:
        raise SampleObservationError("sample-av-provider-error", "Gemini HTTP 500")

    monkeypatch.setattr(router, "resolve_av_sample_windows", fake_av)
    with pytest.raises(SampleObservationError) as exc_info:
        router.resolve_routed_sample_windows(
            _full_ir(), tmp_path, "ep-1", route_override="audiovisual"
        )
    assert exc_info.value.code == "sample-av-provider-error"
