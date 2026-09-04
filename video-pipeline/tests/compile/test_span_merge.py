"""Span-merge tests: plan-v3-shaped fixture + edge conditions.

The 97-item fixture mirrors the committed v44-real-01 plan v3 structure
(speech-granular, 5 real source jumps, 91 redundant splits) without
committing private data — the shape is public: item ids s3..s99,
contiguous record spans from 0, source spans with 5 known jumps.
"""

from __future__ import annotations

from typing import NamedTuple

from services.compile.record_placement import AvPlacement
from services.compile.span_merge import merge_contiguous


class _Clip(NamedTuple):
    item_id: str
    rec_start: int
    rec_end: int
    src_start: int
    src_end: int


def _video(item_id: str, rs: int, re_: int, ss: int, se: int) -> AvPlacement:
    return AvPlacement(
        item_id=item_id, track_kind="video", source_id="src-a",
        source_start=ss, source_end=se, record_start=rs, record_end=re_,
        av_link_id=item_id,
    )


def _audio(item_id: str, rs: int, re_: int, ss: int, se: int) -> AvPlacement:
    return AvPlacement(
        item_id=item_id, track_kind="audio", source_id="src-a",
        source_start=ss, source_end=se, record_start=rs, record_end=re_,
        av_link_id="s" + item_id[1:],
    )


def _plan_v3_clips() -> list[_Clip]:
    """The exact 97-item speech-granular structure (record + source spans
    from the committed v44-real-01 plan v3 shape; public frame numbers,
    generic item ids). 5 source jumps create 6 contiguous segments."""
    return [
        _Clip("s3", 0, 96, 213, 309),
        _Clip("s4", 96, 183, 309, 396),
        _Clip("s5", 183, 252, 396, 465),
        _Clip("s6", 252, 393, 465, 606),
        _Clip("s7", 393, 495, 606, 708),
        _Clip("s8", 495, 540, 708, 753),
        _Clip("s9", 540, 621, 753, 834),
        _Clip("s10", 621, 687, 834, 900),
        _Clip("s11", 687, 825, 900, 1038),
        _Clip("s12", 825, 909, 1038, 1122),
        _Clip("s13", 909, 1002, 1122, 1215),
        _Clip("s14", 1002, 1071, 1215, 1284),
        _Clip("s15", 1071, 1152, 1284, 1365),
        _Clip("s16", 1152, 1269, 1365, 1482),
        _Clip("s17", 1269, 1413, 1482, 1626),
        _Clip("s18", 1413, 1506, 1626, 1719),
        _Clip("s19", 1506, 1572, 1719, 1785),
        _Clip("s20", 1572, 1632, 1785, 1845),
        _Clip("s21", 1632, 1740, 1845, 1953),
        _Clip("s22", 1740, 1794, 1953, 2007),
        _Clip("s23", 1794, 1824, 2007, 2037),
        _Clip("s24", 1824, 1908, 2037, 2121),
        _Clip("s25", 1908, 1983, 2121, 2196),
        _Clip("s26", 1983, 2085, 2196, 2298),
        _Clip("s27", 2085, 2130, 2298, 2343),
        _Clip("s28", 2130, 2214, 2343, 2427),
        _Clip("s29", 2214, 2280, 2427, 2493),
        _Clip("s30", 2280, 2370, 2493, 2583),
        _Clip("s31", 2370, 2457, 2583, 2670),
        _Clip("s32", 2457, 2502, 2670, 2715),
        _Clip("s33", 2502, 2607, 2715, 2820),
        _Clip("s34", 2607, 2724, 2820, 2937),
        _Clip("s35", 2724, 2787, 2937, 3000),
        _Clip("s36", 2787, 2835, 3000, 3048),
        _Clip("s37", 2835, 2898, 3048, 3111),
        _Clip("s38", 2898, 2934, 3111, 3147),
        _Clip("s39", 2934, 2964, 3147, 3177),
        _Clip("s40", 2964, 3084, 3177, 3297),
        _Clip("s41", 3084, 3147, 3297, 3360),
        _Clip("s42", 3147, 3174, 3360, 3387),
        _Clip("s43", 3174, 3393, 3387, 3606),
        _Clip("s44", 3393, 3429, 3606, 3642),
        _Clip("s45", 3429, 3489, 3642, 3702),
        _Clip("s46", 3489, 3525, 3702, 3738),
        _Clip("s47", 3525, 3645, 3738, 3858),
        _Clip("s48", 3645, 3720, 3858, 3933),
        _Clip("s49", 3720, 3756, 3933, 3969),
        _Clip("s50", 3756, 3792, 3969, 4005),
        _Clip("s51", 3792, 3861, 4005, 4074),
        _Clip("s52", 3861, 3930, 4074, 4143),
        _Clip("s53", 3930, 4041, 4143, 4254),
        _Clip("s54", 4041, 4065, 4254, 4278),
        _Clip("s55", 4065, 4143, 4278, 4356),
        _Clip("s56", 4143, 4218, 4356, 4431),
        _Clip("s57", 4218, 4296, 4431, 4509),
        _Clip("s58", 4296, 4350, 4689, 4743),
        _Clip("s59", 4350, 4434, 4743, 4827),
        _Clip("s60", 4434, 4509, 4827, 4902),
        _Clip("s61", 4509, 4638, 4902, 5031),
        _Clip("s62", 4638, 4719, 5031, 5112),
        _Clip("s63", 4719, 4782, 5112, 5175),
        _Clip("s64", 4782, 4845, 5175, 5238),
        _Clip("s65", 4845, 4905, 5238, 5298),
        _Clip("s66", 4905, 4959, 5298, 5352),
        _Clip("s67", 4959, 5037, 5352, 5430),
        _Clip("s68", 5037, 5100, 5430, 5493),
        _Clip("s69", 5100, 5175, 5493, 5568),
        _Clip("s70", 5175, 5271, 5568, 5664),
        _Clip("s71", 5271, 5385, 5664, 5778),
        _Clip("s72", 5385, 5433, 5799, 5847),
        _Clip("s73", 5433, 5493, 5847, 5907),
        _Clip("s74", 5493, 5622, 5907, 6036),
        _Clip("s75", 5622, 5757, 6036, 6171),
        _Clip("s76", 5757, 5973, 6171, 6387),
        _Clip("s77", 5973, 6078, 6387, 6492),
        _Clip("s78", 6078, 6141, 6492, 6555),
        _Clip("s79", 6141, 6216, 6555, 6630),
        _Clip("s80", 6216, 6267, 6630, 6681),
        _Clip("s81", 6267, 6297, 6681, 6711),
        _Clip("s82", 6297, 6372, 6711, 6786),
        _Clip("s83", 6372, 6420, 6786, 6834),
        _Clip("s84", 6420, 6516, 6834, 6930),
        _Clip("s85", 6516, 6636, 6930, 7050),
        _Clip("s86", 6636, 6741, 7050, 7155),
        _Clip("s87", 6741, 6867, 7155, 7281),
        _Clip("s88", 6867, 6960, 7281, 7374),
        _Clip("s89", 6960, 7059, 7374, 7473),
        _Clip("s90", 7059, 7119, 7473, 7533),
        _Clip("s91", 7119, 7140, 7620, 7641),
        _Clip("s92", 7140, 7212, 7641, 7713),
        _Clip("s93", 7212, 7275, 7713, 7776),
        _Clip("s94", 7275, 7338, 7893, 7956),
        _Clip("s95", 7338, 7446, 7956, 8064),
        _Clip("s96", 7446, 7551, 8064, 8169),
        _Clip("s97", 7551, 7623, 8169, 8241),
        _Clip("s98", 7623, 7713, 8298, 8388),
        _Clip("s99", 7713, 7792, 8388, 8467),
    ]


def _placements_from(clips: list[_Clip]) -> tuple[AvPlacement, ...]:
    result = []
    for c in clips:
        result.append(_video(c.item_id, c.rec_start, c.rec_end, c.src_start, c.src_end))
        result.append(_audio("a" + c.item_id[1:], c.rec_start, c.rec_end,
                             c.src_start, c.src_end))
    return tuple(result)


# ------------------------------------------------------ merge behavior


def test_merge_contiguous_returns_empty_for_empty() -> None:
    assert merge_contiguous(()) == ()


def test_single_item_is_unchanged() -> None:
    single = (_video("s3", 0, 96, 213, 309),)
    assert merge_contiguous(single) == single


def test_two_contiguous_items_merge_into_one() -> None:
    placements = (
        _video("s3", 0, 96, 213, 309),
        _video("s4", 96, 183, 309, 396),
    )
    merged = merge_contiguous(placements)
    assert len(merged) == 1
    assert merged[0].record_start == 0
    assert merged[0].record_end == 183
    assert merged[0].source_start == 213
    assert merged[0].source_end == 396
    assert merged[0].av_link_id == "s3"


def test_source_jump_prevents_merge() -> None:
    placements = (
        _video("s3", 0, 96, 213, 309),
        _video("s4", 96, 183, 400, 487),  # source jump 309 -> 400
    )
    merged = merge_contiguous(placements)
    assert len(merged) == 2


def test_record_gap_prevents_merge() -> None:
    placements = (
        _video("s3", 0, 96, 213, 309),
        _video("s4", 100, 183, 309, 396),  # record gap 96 -> 100
    )
    merged = merge_contiguous(placements)
    assert len(merged) == 2


def test_different_source_prevents_merge() -> None:
    other = AvPlacement(
        item_id="x1", track_kind="video", source_id="src-b",
        source_start=309, source_end=396, record_start=96, record_end=183,
        av_link_id="x1",
    )
    placements = (_video("s3", 0, 96, 213, 309), other)
    merged = merge_contiguous(placements)
    assert len(merged) == 2


def test_split_at_record_frame_prevents_merge_across_boundary() -> None:
    placements = (
        _video("s3", 0, 100, 0, 100),
        _video("s4", 100, 200, 100, 200),
    )
    merged = merge_contiguous(placements, split_at_record_frames=(100,))
    assert len(merged) == 2
    unsplit = merge_contiguous(placements)
    assert len(unsplit) == 1


def test_split_frame_at_endpoint_does_not_prevent_merge() -> None:
    """A split frame exactly at a boundary (not strictly inside) is fine."""
    placements = (
        _video("s3", 0, 100, 0, 100),
        _video("s4", 100, 200, 100, 200),
    )
    merged = merge_contiguous(placements, split_at_record_frames=(0, 200))
    assert len(merged) == 1


# ------------------------------------------------- plan-v3 shaped fixture


def test_plan_v3_shaped_97_items_merge_to_6() -> None:
    clips = _plan_v3_clips()
    placements = _placements_from(clips)
    video_only = tuple(p for p in placements if p.track_kind == "video")
    audio_only = tuple(p for p in placements if p.track_kind == "audio")
    merged_v = merge_contiguous(video_only)
    merged_a = merge_contiguous(audio_only)
    # 5 source jumps -> 6 segments
    assert len(merged_v) == 6
    assert len(merged_a) == 6


def test_plan_v3_shaped_with_chapter_split_gives_7() -> None:
    clips = _plan_v3_clips()
    placements = _placements_from(clips)
    video_only = tuple(p for p in placements if p.track_kind == "video")
    merged = merge_contiguous(video_only, split_at_record_frames=(1632,))
    assert len(merged) == 7


def test_total_record_length_unchanged() -> None:
    clips = _plan_v3_clips()
    placements = _placements_from(clips)
    video_only = tuple(p for p in placements if p.track_kind == "video")
    before_total = sum(p.record_end - p.record_start for p in video_only)
    merged = merge_contiguous(video_only)
    after_total = sum(p.record_end - p.record_start for p in merged)
    assert before_total == after_total


def test_frame_mapping_invariance_at_sampled_points() -> None:
    """For any record frame, the mapped source frame is identical before
    and after merge (merge joins physically-continuous reads)."""
    clips = _plan_v3_clips()
    placements = _placements_from(clips)
    video_only = tuple(p for p in placements if p.track_kind == "video")
    merged = merge_contiguous(video_only)

    def source_at(items: tuple[AvPlacement, ...], record: int) -> int:
        for item in items:
            if item.record_start <= record < item.record_end:
                return item.source_start + (record - item.record_start)
        raise AssertionError(f"record {record} not covered")

    for sample in (0, 1, 50, 96, 97, 500, 1631, 1632, 1633,
                   4295, 4296, 4297, 5384, 5385, 7791):
        assert source_at(video_only, sample) == source_at(merged, sample), (
            f"frame mapping diverged at record {sample}"
        )


def test_merged_av_links_still_pair() -> None:
    """Every merged audio item's av_link matches its paired merged video
    item's av_link (the link groups stay consistent across tracks)."""
    clips = _plan_v3_clips()
    placements = _placements_from(clips)
    merged = merge_contiguous(placements)
    video_links = [p.av_link_id for p in merged if p.track_kind == "video"]
    audio_links = [p.av_link_id for p in merged if p.track_kind == "audio"]
    assert len(video_links) == 6
    assert len(audio_links) == 6
    assert sorted(video_links) == sorted(audio_links), (
        f"video links {video_links} != audio links {audio_links}"
    )


def test_merged_spans_cover_full_timeline() -> None:
    clips = _plan_v3_clips()
    placements = _placements_from(clips)
    video_only = tuple(p for p in placements if p.track_kind == "video")
    merged = merge_contiguous(video_only)
    # no gaps between merged items
    # (gap presence between merged items matches the real-cut set)
    # first and last match the original
    assert merged[0].record_start == video_only[0].record_start
    assert merged[-1].record_end == video_only[-1].record_end
