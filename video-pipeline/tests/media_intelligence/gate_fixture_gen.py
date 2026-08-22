"""Deterministic synthetic fixtures for Gate V43-1 (plan task 19).

Three materially different source-set classes, built programmatically with
zero randomness — identical inputs yield byte-identical canonical
``MediaIntelligenceArtifact`` JSON (the committed ``artifact.json`` files
are regenerated from these builders and locked byte-for-byte by
``test_gate_v43_1.py``).

Coverage arithmetic (drives the ≤25% deep-review SLO under the gate policy
``ProgressivePolicy(recall_audit_sample_count=2)``):

- speaker-broll: windows = 2x broll + reaction + recall(pause)      = 360/1800 = 20%
- visual-first:  windows = 4x hero(90) + recall(2x 150)             = 660/2910 ~ 22.7%
- screen-product: windows = screen + demo + insert + recall(outro)  = 510/2490 ~ 20.5%

The mutation/reanalysis and trigger-reason diversity rely on these exact
spans and select potentials — change them and re-run the gate test.
"""

from __future__ import annotations

from pathlib import Path

from services.foundation_io import canonical_model_bytes
from services.media_intelligence.models import (
    EditSourceSpan,
    FaceReactionCue,
    MediaIntelligenceArtifact,
    MediaSource,
    ObjectRef,
    ProductRef,
    ProvenanceRecord,
    Shot,
    ShotBestMoment,
    ShotConfidence,
    ShotCuttability,
    ShotEditorial,
    ShotVisual,
    SimilarityRef,
    SpeakerInfo,
    SubjectAction,
    TranscriptSegment,
    TranscriptWordTiming,
    VisibleText,
)

_FIXTURE_IDS = ("speaker-broll", "visual-first", "screen-product")


def _shot(  # noqa: PLR0913 (fixture DSL: one knob per PRD 7.2 field group)
    shot_id: str,
    start: int,
    end: int,
    *,
    role: str,
    select: str,
    desc: str,
    why: str,
    size: str = "medium",
    motion: str = "static",
    transcript: tuple[str, ...] = (),
    speaker: bool = False,
    words: bool = False,
    objects: tuple[str, ...] = (),
    product: str | None = None,
    visible: tuple[str, ...] = (),
    subject: tuple[str, str] | None = None,
    face: str | None = None,
    sim: tuple[tuple[str, float], ...] = (),
) -> Shot:
    mid = (start + end) // 2
    return Shot(
        shot_id=shot_id,
        source_span=EditSourceSpan(start_frame=start, end_frame=end),
        description=desc,
        visual=ShotVisual(shot_size=size, camera_motion=motion),
        editorial=ShotEditorial(
            role=role,
            select_potential=select,
            best_moment=ShotBestMoment(frame=mid, why=why),
            pacing="moderate",
            cuttability=ShotCuttability(in_="clean", out="clean"),
        ),
        confidence=ShotConfidence(editorial="high", visual="high"),
        transcript_segments=(
            tuple(
                TranscriptSegment(
                    segment_id=f"seg-{shot_id[-2:]}",
                    text=text,
                    start_frame=start + 10,
                    end_frame=end - 10,
                    words=(
                        (
                            TranscriptWordTiming(
                                word=text.split()[0],
                                start_frame=start + 10,
                                end_frame=start + 20,
                            ),
                            TranscriptWordTiming(
                                word=text.split()[-1],
                                start_frame=start + 20,
                                end_frame=end - 10,
                            ),
                        )
                        if words
                        else None
                    ),
                )
                for text in transcript
            )
            or None
        ),
        speaker_info=SpeakerInfo(speaker_id="speaker-01", display_name="Speaker")
        if speaker
        else None,
        visible_texts=tuple(VisibleText(text=t, frame=mid) for t in visible) or None,
        subject_action=SubjectAction(primary_subject=subject[0], action=subject[1])
        if subject
        else None,
        object_refs=tuple(ObjectRef(label=label, frame=mid) for label in objects) or None,
        product_refs=(ProductRef(product_id=product, frame=mid),) if product else None,
        face_reaction_cues=(FaceReactionCue(cue=face, frame=mid, confidence="high"),)
        if face
        else None,
        similarity_refs=tuple(SimilarityRef(ref_shot_id=ref, score=score) for ref, score in sim)
        or None,
        provenance=(
            ProvenanceRecord(
                field="synthesis",
                provider="gate-fixture-gen",
                provider_version="1.0.0",
                confidence="deterministic",
            ),
        ),
    )


def _artifact(
    episode: str, source: str, duration: int, shots: list[Shot]
) -> MediaIntelligenceArtifact:
    return MediaIntelligenceArtifact(
        episode_id=episode,
        sources=(
            MediaSource(
                source_id=source,
                duration_frames=duration,
                path=f"synthetic://gate-v43-1/{episode}/{source}",
            ),
        ),
        shots=tuple(shots),
    )


def speaker_broll() -> MediaIntelligenceArtifact:
    talk = [
        _shot(
            f"s{i:02d}",
            180 * (i - 1),
            180 * i,
            role="talking_head",
            select="medium",
            desc=f"トークパート{i}: 撮影機材の選び方を語る",
            why=f"話題{i}の結論がまとまった瞬間",
            transcript=(f"今日は機材の選び方その{i}について話します。",),
            speaker=True,
            words=True,
        )
        for i in range(1, 9)
    ]
    broll = [
        _shot(
            "s09",
            1440,
            1500,
            role="broll",
            select="low",
            desc="B-roll: 渋谷の交差点 歩行者シグナルと人波",
            why="交差点の人流が話題の導入と重なる",
            objects=("scramble_crossing",),
            subject=("crossing", "pedestrian flow"),
        ),
        _shot(
            "s10",
            1500,
            1560,
            role="broll",
            select="low",
            desc="B-roll: カフェの窓から見える街並みと通行人",
            why="窓越しの街並みで落ち着いた文脈を作る",
            objects=("cafe_window",),
            subject=("cafe window", "street view"),
        ),
    ]
    reaction = _shot(
        "s11",
        1560,
        1620,
        role="reaction",
        select="medium",
        desc="ゲストの笑顔リアクション",
        why="機材の失敗談に対する大きな笑い",
        face="laugh",
    )
    pause = _shot(
        "s12",
        1620,
        1800,
        role="pause",
        select="low",
        desc="機材確認の無音時間",
        why="作業風景の静かな区間",
        sim=(("s01", 0.8), ("s02", 0.8)),
    )
    return _artifact("gate43-speaker-broll", "src-cam-a", 1800, [*talk, *broll, reaction, pause])


def visual_first() -> MediaIntelligenceArtifact:
    intro = _shot(
        "v01",
        0,
        150,
        role="talking_head",
        select="low",
        desc="収録の挨拶: 行ってきます",
        why="冒頭の一言で旅の始まりを示す",
        transcript=("行ってきます。",),
        speaker=True,
    )
    heroes = [
        _shot(
            "v02",
            150,
            240,
            role="action",
            select="high",
            desc="サーフィンの初ターン、水しぶきが上がる",
            why="初めてのターンが決まり水しぶきが上がる",
            visible=("大会番号 07",),
            objects=("surfboard",),
            subject=("surfer", "first wave turn"),
            motion="handheld",
        ),
        _shot(
            "v03",
            240,
            330,
            role="establishing",
            select="high",
            desc="崖の上からの海岸線パノラマ",
            why="夕日に照らされた海岸線のパン",
            objects=("coastline",),
            subject=("cliff", "panorama reveal"),
            size="wide",
            motion="pan",
        ),
        _shot(
            "v04",
            330,
            420,
            role="action",
            select="high",
            desc="カヤックの急流下り、水が画面を覆う",
            why="急流下りで水しぶきが画面を覆う",
            objects=("kayak", "helmet"),
            subject=("kayak", "river descent"),
            motion="handheld",
        ),
        _shot(
            "v05",
            420,
            510,
            role="establishing",
            select="high",
            desc="灯台のシルエットと夕景",
            why="灯台のシルエットが夕景に重なる",
            visible=("灯台の案内板",),
            subject=("lighthouse", "sunset silhouette"),
            size="wide",
        ),
    ]
    scenery = [
        _shot(
            "v06",
            510,
            660,
            role="scenery",
            select="low",
            desc="海開けの風景 ワイド",
            why="水平線が開ける静かなワイド",
            size="wide",
            sim=(("v02", 0.7), ("v03", 0.7), ("v04", 0.7)),
        ),
        _shot(
            "v07",
            660,
            810,
            role="scenery",
            select="low",
            desc="夕暮れの水平線",
            why="日没後の残光が残る水平線",
            size="wide",
            sim=(("v03", 0.7), ("v06", 0.7), ("v02", 0.7)),
        ),
        *(
            _shot(
                f"v{i:02d}",
                810 + 700 * (i - 8),
                1510 + 700 * (i - 8),
                role="scenery",
                select="low",
                desc=f"夜の浜辺の長回し{i - 7}",
                why=f"波音だけの長回し{i - 7}",
                size="wide",
                sim=(("v03", 0.7),),
            )
            for i in (8, 9, 10)
        ),
    ]
    return _artifact("gate43-visual-first", "src-cam-b", 2910, [intro, *heroes, *scenery])


def screen_product() -> MediaIntelligenceArtifact:
    talk = [
        _shot(
            f"c{i:02d}",
            span[0],
            span[1],
            role="talking_head",
            select="medium",
            desc=f"製品解説パート{i}: gadget-x1 の紹介",
            why=f"用途{i}の説明が一段落する瞬間",
            transcript=(f"gadget-x1 のここがすごい、その{i}を紹介します。",),
            speaker=True,
            words=True,
        )
        for i, span in enumerate(
            [(0, 330), (570, 900), (990, 1320), (1320, 1650), (1650, 1980), (1980, 2310)],
            start=1,
        )
    ]
    screen = _shot(
        "c02-s",
        330,
        450,
        role="screen_demo",
        select="high",
        desc="画面紹介: 設定メニューの Walkthrough",
        why="PRO SETTINGS 項目が順に示される",
        visible=("設定メニュー", "PRO SETTINGS"),
        subject=("app UI", "menu walkthrough"),
        size="full",
        motion="screen_capture",
    )
    demo = _shot(
        "c03-s",
        450,
        570,
        role="demonstration",
        select="high",
        desc="製品デモ: gadget-x1 のダイヤル操作",
        why="ダイヤル操作で即座に値が変わる瞬間",
        product="prod-ref-01",
        objects=("gadget-x1",),
        subject=("presenter", "product demo"),
        motion="handheld",
    )
    insert = _shot(
        "c05-s",
        900,
        990,
        role="insert",
        select="medium",
        desc="インサート: ダイヤルのクローズアップ",
        why="ダイヤルの刻みが見えるクローズアップ",
        objects=("gadget-x1 dial",),
        subject=("close-up", "dial rotation"),
        size="close",
    )
    outro = _shot(
        "c10-s",
        2310,
        2490,
        role="outro",
        select="low",
        desc="アウトロのまとめ-shot",
        why="紹介のまとめと視線の着地",
        sim=(("c01", 0.75), ("c02-s", 0.75)),
    )
    ordered = [talk[0], screen, demo, talk[1], insert, *talk[2:], outro]
    return _artifact("gate43-screen-product", "src-cam-c", 2490, ordered)


_BUILDERS = {
    "speaker-broll": speaker_broll,
    "visual-first": visual_first,
    "screen-product": screen_product,
}


def build_fixture(fixture_id: str) -> MediaIntelligenceArtifact:
    """Deterministically rebuild one fixture artifact (no randomness)."""
    return _BUILDERS[fixture_id]()


def canonical_fixture_bytes(fixture_id: str) -> bytes:
    """Canonical bytes of a fixture — the committed artifact.json content."""
    return canonical_model_bytes(build_fixture(fixture_id))


def write_fixtures(root: Path) -> dict[str, Path]:
    """Write/refresh every fixture artifact.json under ``root`` (idempotent)."""
    written: dict[str, Path] = {}
    for fixture_id in _FIXTURE_IDS:
        path = Path(root) / fixture_id / "artifact.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_fixture_bytes(fixture_id))
        written[fixture_id] = path
    return written


__all__ = [
    "build_fixture",
    "canonical_fixture_bytes",
    "write_fixtures",
]


if __name__ == "__main__":  # pragma: no cover - manual regeneration entry
    root = Path(__file__).parent / "fixtures" / "gate-v43-1"
    for fixture_id, path in write_fixtures(root).items():
        print(f"wrote {path} ({path.stat().st_size} bytes) for {fixture_id}")
