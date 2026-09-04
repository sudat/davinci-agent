"""WBS-2 live telop handler: nested cards on V3 through the pinned MCP.

Scripted-transport mirror of the subtitle-card tests in
``test_live_adapter.py`` (same fake raw transport, same probe shapes).
The binding resolution is stubbed here — the profile→input mapping is
locked independently in ``test_telop_style.py`` — so these tests freeze
the MECHANICS: V3 ensure, tile batching, half-open absolute placement,
one final track scan, and the through-nesting readback mirror.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, cast, get_args

import pytest

from services.mcp_execution.live_adapter import (
    SUPPORTED_SURFACES,
    LiveAdapterError,
    LiveMcpAdapter,
)
from services.mcp_execution.live_handlers import HANDLERS, telop
from services.mcp_execution.live_handlers.telop_card import (
    TELOP_TEMPLATE,
    TELOP_TEMPLATE_ASSET,
    TELOP_TEMPLATE_SHA256,
)
from services.mcp_execution.live_handlers.telop_spans import tile_spans
from services.mcp_execution.live_handlers.telop_style import TelopCardBinding
from services.mcp_execution.plan_models import ToolSurface

TIMELINE_NAME = "ep-telop-timeline"
TIMELINE_START = 108000  # 01:00:00:00 @30fps
CHAPTER_GAP = (1632, 1677)

#: WBS-1 probe check-c wire values for the stubbed binding (round-trip set).
#: CharacterSpacing follows the write-path bisect 2026-09-04 (P3 = 1.0).
STUB_INPUTS: dict[str, object] = {
    "StyledText": "スタブテロップ",
    "Font": "Hiragino Sans W6",
    "Size": 0.0352,
    "TextPos": [0.1849, 0.9324],
    "CharacterSpacing": 1.0,
    "OutlineEnabled": 1,
    "OutlineThickness": 0.0019,
    "OutlineSoftness": 0,
    "OutlineR": 0.0627,
    "OutlineG": 0.0627,
    "OutlineB": 0.0627,
    "OutlineA": 1.0,
    "BandR": 0.0392,
    "BandG": 0.0392,
    "BandB": 0.0392,
    "BandAlpha": 0.549,
    "BandWidth": 0.3354,
    "BandHeight": 0.1074,
    "BandPos": [0.1849, 0.9324],
}


class ScriptedTransport:
    """Fake raw McpTransportFn asserting vendor tool names only."""

    REAL_TOOLS = frozenset(
        {
            "project_manager",
            "project_settings",
            "media_pool",
            "timeline",
            "timeline_item",
            "fusion_comp",
            "resolve_control",
            "render",
            "timeline_item_color",
        }
    )

    def __init__(self, script: Mapping[tuple[str, str], list[dict[str, Any]]]) -> None:
        self._script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def __call__(
        self,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        if tool_name not in self.REAL_TOOLS:
            raise AssertionError(f"raw call used logical surface {tool_name!r}")
        self.calls.append((tool_name, action, dict(normalized_params)))
        key = (tool_name, action)
        if key not in self._script or not self._script[key]:
            raise AssertionError(f"unexpected raw call {key} with {normalized_params}")
        queue = self._script[key]
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]


def _telop_params() -> dict[str, object]:
    return {
        "action": "apply_telop",
        "cards": [
            {
                "card_id": "telop-opening",
                "kind": "opening",
                "text": "冒頭テロップ",
                "record_span": {"start_frame": 0, "end_frame": 90},
            },
            {
                "card_id": "telop-persistent",
                "kind": "persistent",
                "text": "常時表示テロップ",
                "record_span": {"start_frame": 90, "end_frame": 7837},
            },
            {
                "card_id": "telop-chapter",
                "kind": "chapter",
                "text": "章タイトル",
                "record_span": {
                    "start_frame": CHAPTER_GAP[0],
                    "end_frame": CHAPTER_GAP[1],
                },
            },
        ],
        "style_profile_id": "telop/default",
    }


def _expected_abs_spans() -> list[tuple[int, int]]:
    spans = [(TIMELINE_START, TIMELINE_START + 90)]
    spans += [
        (TIMELINE_START + s, TIMELINE_START + e)
        for s, e in tile_spans((90, 7837), (CHAPTER_GAP,), 150)
    ]
    spans.append((TIMELINE_START + CHAPTER_GAP[0], TIMELINE_START + CHAPTER_GAP[1]))
    return spans


def _rows(spans: list[tuple[int, int]]) -> dict[str, Any]:
    return {
        "items": [
            {"name": f"row-{i}", "id": f"ti-{i}", "start": s, "end": e}
            for i, (s, e) in enumerate(spans)
        ]
    }


def _set_inputs_response() -> dict[str, Any]:
    return {
        "success": True,
        "tool_name": "Template",
        "results": {k: {"success": True, "value": v} for k, v in STUB_INPUTS.items()},
    }


def _prefix_response() -> dict[str, Any]:
    return {
        "success": True,
        "tool_name": "Text",
        "results": {
            axis: {"success": True, "value": 0} for axis in ("Red", "Green", "Blue", "Alpha")
        },
    }


_GET_FONT = {"value": "Hiragino Sans W6"}
_GET_SIZE = {"value": 0.0352}
_GET_POS = {"value": {"1": 0.1849, "2": 0.9324, "3": 0.0}}
_GET_TRIO = [_GET_FONT, _GET_SIZE, _GET_POS]


def _make_prepared_adapter() -> tuple[LiveMcpAdapter, ScriptedTransport]:
    transport = ScriptedTransport(
        {
            ("project_manager", "load"): [{"success": False}],
            ("project_manager", "create"): [{"name": TIMELINE_NAME, "success": True}],
            ("project_settings", "set_setting"): [{"success": True}],
            ("project_settings", "get_setting"): [
                {"settings": "smart", "success": True}
            ],
            ("media_pool", "create_timeline"): [
                {"name": TIMELINE_NAME, "id": "tl-main", "success": True}
            ],
            ("timeline", "set_current"): [{"success": True}],
            ("timeline", "get_current"): [
                {
                    "name": TIMELINE_NAME,
                    "id": "tl-main",
                    "start_frame": TIMELINE_START,
                    "end_frame": TIMELINE_START,
                    "success": True,
                }
            ],
        }
    )
    adapter = LiveMcpAdapter(transport, media_paths={})
    adapter(
        "prepare_project",
        "prepare_project",
        {
            "action": "prepare_project",
            "timeline_name": TIMELINE_NAME,
            "fps_num": 30,
            "fps_den": 1,
        },
    )
    transport.calls.clear()
    return adapter, transport


@pytest.fixture
def stub_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    def _resolve(
        kind: str,
        text: str,
        style: object,
        anchor_band: object = None,
    ) -> TelopCardBinding:
        return TelopCardBinding(
            styled_text="スタブテロップ",
            font="Hiragino Sans W6",
            size=0.0352,
            text_pos=(0.1849, 0.9324),
            inputs=dict(STUB_INPUTS),
        )

    monkeypatch.setattr(telop, "resolve_telop_binding", _resolve)


def _script_creation(transport: ScriptedTransport) -> None:
    transport._script[("timeline", "set_current")] = [{"success": True}] * 13
    transport._script[("timeline", "get_track_count")] = [{"count": 2, "success": True}]
    transport._script[("timeline", "add_track")] = [{"success": True}]
    transport._script[("timeline", "get_items_in_track")] = [
        _rows([]),
        _rows(_expected_abs_spans()),
    ]
    transport._script[("media_pool", "create_timeline")] = [
        {"name": f"{TIMELINE_NAME}-telop-{cid}", "id": f"tl-{cid}", "success": True}
        for cid in ("telop-opening", "telop-persistent", "telop-chapter")
    ]
    transport._script[("timeline", "insert_fusion_title")] = [{"success": True}] * 3
    transport._script[("fusion_comp", "safe_set_inputs")] = [
        _prefix_response(),
        _set_inputs_response(),
    ] * 3
    transport._script[("fusion_comp", "get_text_plus")] = [
        {"tool_name": "Template", "input_name": "StyledText", "text": "スタブテロップ"}
    ] * 6
    transport._script[("fusion_comp", "get_input")] = _GET_TRIO * 6
    transport._script[("timeline", "get_media_pool_item")] = [
        {"name": f"{TIMELINE_NAME}-telop-{cid}", "id": f"mpi-{cid}"}
        for cid in ("telop-opening", "telop-persistent", "telop-chapter")
    ]
    transport._script[("media_pool", "append_to_timeline")] = [
        {"count": 1, "items": [], "success": True, "verification_status": "readback_verified"},
        {"count": 11, "items": [], "success": True, "verification_status": "readback_verified"},
        {"count": 42, "items": [], "success": True, "verification_status": "readback_verified"},
        {"count": 1, "items": [], "success": True, "verification_status": "readback_verified"},
    ]


def _clip_infos(call: tuple[str, str, dict[str, object]]) -> list[dict[str, object]]:
    raw = call[2].get("clip_infos")
    assert isinstance(raw, list)
    return [cast("dict[str, object]", item) for item in raw if isinstance(item, dict)]


def _actions(transport: ScriptedTransport, action: str) -> list[tuple[str, str, dict[str, object]]]:
    return [call for call in transport.calls if call[1] == action]


# ------------------------------------------------------------- registration


def test_telop_surface_is_registered_on_every_frozen_authority() -> None:
    # Given: the WBS-2 registration requirement
    # Then: the surface exists in the ToolSurface vocabulary, the adapter's
    #       support authority, and the handler registry routes apply_telop
    assert "telop_generation_probe" in get_args(ToolSurface)
    assert "telop_generation_probe" in SUPPORTED_SURFACES
    assert HANDLERS["telop_generation_probe"] is telop.apply_telop


def test_tracked_template_asset_is_the_probe_proven_bytes() -> None:
    # Given: the tracked product template asset (revision 2: white-render fix)
    # When: hashing the tracked file
    # Then: bytes match the pinned revision — MainOutput on the band Merge,
    #       no KeyStretcher (structural invariants: test_telop_template_asset);
    #       superseded revision 1 (WBS-1 probe copy, KeyStretcher MainOutput):
    #       b1c6683c2422681120dcdaa91f2f610fa6af9401ab81f5d85a638c3669e3c1ed
    digest = hashlib.sha256(TELOP_TEMPLATE_ASSET.read_bytes()).hexdigest()
    assert digest == TELOP_TEMPLATE_SHA256
    assert TELOP_TEMPLATE_SHA256 == (
        "a18b90f83adcf8d1de554bfc07fb81cbd03484504a8e329d7b5cbd57f09b937a"
    )
    assert TELOP_TEMPLATE_ASSET.stem == TELOP_TEMPLATE


# --------------------------------------------------------------- creation


def test_telop_creation_places_nested_cards_on_v3(stub_binding: None) -> None:
    # Given: a prepared adapter and the three committed telop cards
    # When: applying telop natively
    # Then: per-card evidence returns with exact text/spans, the timeline is
    #       marked mutated, and only vendor tools were called
    adapter, transport = _make_prepared_adapter()
    _script_creation(transport)

    result = cast(
        "dict[str, object]", adapter("telop_generation_probe", "apply_telop", _telop_params())
    )

    cards = cast("list[dict[str, object]]", result.get("cards"))
    assert [card["card_id"] for card in cards] == [
        "telop-opening",
        "telop-persistent",
        "telop-chapter",
    ]
    assert adapter.timeline_mutated is True
    assert {call[0] for call in transport.calls} <= ScriptedTransport.REAL_TOOLS
    # The product template is inserted by its exact Effects Library name
    inserts = _actions(transport, "insert_fusion_title")
    assert len(inserts) == 3
    assert all(call[2] == {"name": "FVP Telop Band v1"} for call in inserts)
    # safe_set_inputs: per card the direct Text white-removal prefix,
    # then the Template group tool with the probe key set
    styled = _actions(transport, "safe_set_inputs")
    assert len(styled) == 6
    groups = styled[1::2]
    assert all(call[2]["tool_name"] == "Template" for call in groups)
    written = cast("dict[str, object]", groups[0][2]["inputs"])
    assert set(written) == set(STUB_INPUTS)
    # V3 was ensured once (count 2 → one add) and scanned exactly twice:
    # the rerun snapshot and ONE final fresh verification scan
    assert _actions(transport, "add_track") == [("timeline", "add_track", {"track_type": "video"})]
    scans = _actions(transport, "get_items_in_track")
    assert len(scans) == 2
    assert all(call[2] == {"track_type": "video", "track_index": 3} for call in scans)


def test_telop_creation_clears_text_background_before_group_writes(
    stub_binding: None,
) -> None:
    # Given: the tracked band template's baked white Text background
    #        (write-path bisect 2026-09-04: group route cannot reach it)
    # When: each card is created
    # Then: the FIRST write per card is the direct Text-tool prefix with
    #       all four RGBA axes at 0, and the 19-input group write follows
    adapter, transport = _make_prepared_adapter()
    _script_creation(transport)

    adapter("telop_generation_probe", "apply_telop", _telop_params())

    writes = _actions(transport, "safe_set_inputs")
    assert len(writes) == 6
    for card_idx in range(3):
        prefix = writes[card_idx * 2]
        group = writes[card_idx * 2 + 1]
        assert prefix[2]["tool_name"] == "Text"
        assert prefix[2]["inputs"] == {"Red": 0, "Green": 0, "Blue": 0, "Alpha": 0}
        assert group[2]["tool_name"] == "Template"
        assert set(cast("dict[str, object]", group[2]["inputs"])) == set(STUB_INPUTS)
    # exact per-card interleaving: insert → direct Text prefix → group set
    sequence = [
        (call[1], call[2].get("tool_name"))
        for call in transport.calls
        if call[1] in ("insert_fusion_title", "safe_set_inputs")
    ]
    assert (
        sequence
        == [
            ("insert_fusion_title", None),
            ("safe_set_inputs", "Text"),
            ("safe_set_inputs", "Template"),
        ]
        * 3
    )


def test_telop_creation_failed_background_clear_is_a_typed_failure(
    stub_binding: None,
) -> None:
    # Given: a card whose Text background clear does not read back 0
    # When: the telop mutation runs
    # Then: typed telop-text-background-clear-failed — never a silent
    #       white frame downstream
    adapter, transport = _make_prepared_adapter()
    _script_creation(transport)
    bad_prefix = _prefix_response()
    bad_prefix["results"]["Red"] = {"success": True, "value": 1.0}
    transport._script[("fusion_comp", "safe_set_inputs")] = [
        bad_prefix,
        _set_inputs_response(),
    ] * 3

    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("telop_generation_probe", "apply_telop", _telop_params())
    assert excinfo.value.code == "telop-text-background-clear-failed"


def test_telop_appends_batch_one_call_per_contiguous_tile_run(stub_binding: None) -> None:
    # Given: the representative three-card episode line
    # When: the telop mutation runs
    # Then: exactly four appends — opening(1), persistent run1(11),
    #       persistent run2(42), chapter(1) — every clip on track 3 with
    #       absolute record frames, half-open lengths, card media [0,len)
    adapter, transport = _make_prepared_adapter()
    _script_creation(transport)

    adapter("telop_generation_probe", "apply_telop", _telop_params())

    appends = _actions(transport, "append_to_timeline")
    assert [len(_clip_infos(call)) for call in appends] == [1, 11, 42, 1]
    expected = _expected_abs_spans()
    placed = [clip for call in appends for clip in _clip_infos(call)]
    placed_spans = [
        (
            cast("int", clip["record_frame"]),
            cast("int", clip["record_frame"]) + cast("int", clip["end_frame"]),
        )
        for clip in placed
    ]
    assert placed_spans == expected
    for clip in placed:
        assert clip["track_index"] == 3
        assert clip["record_frame_mode"] == "absolute"
        assert clip["media_type"] == 1
        assert clip["start_frame"] == 0
        assert cast("int", clip["end_frame"]) > 0
    # No tile ever lands inside the chapter gap [1632,1677)
    for clip in placed:
        start = cast("int", clip["record_frame"]) - TIMELINE_START
        end = start + cast("int", clip["end_frame"])
        assert not (start < CHAPTER_GAP[1] and CHAPTER_GAP[0] < end) or (start == CHAPTER_GAP[0])


def _telop_params_with_second_layer() -> dict[str, object]:
    """The D four-card set: the C three cards plus the chapter-name second
    layer [1677,7837) (DESIGN D §5)."""
    params = _telop_params()
    cards = cast("list[dict[str, object]]", params["cards"])
    cards.append(
        {
            "card_id": "telop-persistent-second",
            "kind": "persistent_second",
            "text": "どこにもないカメラバッグ",
            "record_span": {"start_frame": 1677, "end_frame": 7837},
        }
    )
    return params


def _second_layer_abs_spans() -> list[tuple[int, int]]:
    return [
        (TIMELINE_START + s, TIMELINE_START + e)
        for s, e in tile_spans((1677, 7837), (CHAPTER_GAP,), 150)
    ]


def test_telop_second_layer_places_tiles_on_v4(stub_binding: None) -> None:
    # Given: the D four-card set on a 2-track timeline (V1 + V2 subtitle)
    # When: applying telop natively
    # Then: V3 carries the C three cards' spans; V4 carries exactly the 42
    #       second-layer tiles; the ensure adds TWO tracks (V3 then V4,
    #       one per track loop pass) and each track gets its own snapshot
    #       plus ONE fresh verification scan
    adapter, transport = _make_prepared_adapter()
    v3_spans = _expected_abs_spans()
    v4_spans = _second_layer_abs_spans()
    transport._script[("timeline", "set_current")] = [{"success": True}] * 19
    transport._script[("timeline", "get_track_count")] = [
        {"count": 2, "success": True},  # V3 ensure pass
        {"count": 3, "success": True},  # V4 ensure pass
    ]
    transport._script[("timeline", "add_track")] = [{"success": True}] * 2
    transport._script[("timeline", "get_items_in_track")] = [
        _rows([]),  # V3 snapshot (fresh build)
        _rows(v3_spans),  # V3 fresh verification
        _rows([]),  # V4 snapshot
        _rows(v4_spans),  # V4 fresh verification
    ]
    transport._script[("media_pool", "create_timeline")] = [
        {"name": f"{TIMELINE_NAME}-telop-{cid}", "id": f"tl-{cid}", "success": True}
        for cid in (
            "telop-opening",
            "telop-persistent",
            "telop-chapter",
            "telop-persistent-second",
        )
    ]
    transport._script[("timeline", "insert_fusion_title")] = [{"success": True}] * 4
    transport._script[("fusion_comp", "safe_set_inputs")] = [
        _prefix_response(),
        _set_inputs_response(),
    ] * 4
    transport._script[("fusion_comp", "get_text_plus")] = [
        {"tool_name": "Template", "input_name": "StyledText", "text": "スタブテロップ"}
    ] * 8
    transport._script[("fusion_comp", "get_input")] = _GET_TRIO * 8
    transport._script[("timeline", "get_media_pool_item")] = [
        {"name": f"{TIMELINE_NAME}-telop-{cid}", "id": f"mpi-{cid}"}
        for cid in (
            "telop-opening",
            "telop-persistent",
            "telop-chapter",
            "telop-persistent-second",
        )
    ]
    transport._script[("media_pool", "append_to_timeline")] = [
        {"count": 1, "items": [], "success": True, "verification_status": "readback_verified"},
        {"count": 11, "items": [], "success": True, "verification_status": "readback_verified"},
        {"count": 42, "items": [], "success": True, "verification_status": "readback_verified"},
        {"count": 1, "items": [], "success": True, "verification_status": "readback_verified"},
        {"count": 42, "items": [], "success": True, "verification_status": "readback_verified"},
    ]

    result = cast(
        "dict[str, object]",
        adapter("telop_generation_probe", "apply_telop", _telop_params_with_second_layer()),
    )

    cards = cast("list[dict[str, object]]", result.get("cards"))
    assert [card["card_id"] for card in cards] == [
        "telop-opening",
        "telop-persistent",
        "telop-chapter",
        "telop-persistent-second",
    ]
    assert cards[-1]["track_index"] == 4
    assert all(card["track_index"] == 3 for card in cards[:3])
    # one add per ensure pass: V3 then V4
    assert _actions(transport, "add_track") == [
        ("timeline", "add_track", {"track_type": "video"})
    ] * 2
    # appends: opening(1), persistent run1(11), persistent run2(42),
    # chapter(1) on V3; then the second layer's 42-tile run on V4
    appends = _actions(transport, "append_to_timeline")
    assert [len(_clip_infos(call)) for call in appends] == [1, 11, 42, 1, 42]
    v4_clips = _clip_infos(appends[-1])
    assert all(clip["track_index"] == 4 for clip in v4_clips)
    placed_v4 = [
        (
            cast("int", clip["record_frame"]),
            cast("int", clip["record_frame"]) + cast("int", clip["end_frame"]),
        )
        for clip in v4_clips
    ]
    assert placed_v4 == v4_spans
    # two verification scans per track: snapshots (3, 4) then fresh (3, 4)
    scans = _actions(transport, "get_items_in_track")
    assert [scan[2]["track_index"] for scan in scans] == [3, 3, 4, 4]


def test_telop_rerun_all_existing_verifies_without_mutating(stub_binding: None) -> None:
    # Given: every expected span already on V3
    # When: re-applying the same telop params
    # Then: zero creation mutations; each card is re-verified through the
    #       nesting (text + bound inputs); exactly ONE track scan
    adapter, transport = _make_prepared_adapter()
    transport._script[("timeline", "set_current")] = [{"success": True}] * 7
    transport._script[("timeline", "get_track_count")] = [{"count": 3, "success": True}]
    transport._script[("timeline", "get_items_in_track")] = [
        _rows(_expected_abs_spans()),
    ]
    transport._script[("fusion_comp", "get_text_plus")] = [
        {"tool_name": "Template", "input_name": "StyledText", "text": "スタブテロップ"}
    ] * 3
    transport._script[("fusion_comp", "get_input")] = _GET_TRIO * 3

    result = adapter("telop_generation_probe", "apply_telop", _telop_params())

    mutating = {
        ("media_pool", "create_timeline"),
        ("media_pool", "append_to_timeline"),
        ("timeline", "insert_fusion_title"),
        ("fusion_comp", "safe_set_inputs"),
        ("timeline", "add_track"),
    }
    assert mutating.isdisjoint({(c[0], c[1]) for c in transport.calls})
    assert len(_actions(transport, "get_items_in_track")) == 1
    cards = cast("list[dict[str, object]]", cast("dict[str, object]", result).get("cards"))
    assert len(cards) == 3
    assert adapter.timeline_mutated is False


def test_telop_partial_run_tops_up_only_missing_tiles(stub_binding: None) -> None:
    # Given: a retry state where 6 contiguous run-1 tiles are missing but
    #        the card itself already exists on V3
    # When: the same mutation runs again
    # Then: the existing card is opened (no re-creation) and ONE batched
    #       append places exactly the 6 missing spans
    spans = _expected_abs_spans()
    opening = spans[:1]
    run1 = spans[1:12]
    run2 = spans[12:54]
    chapter = spans[54:]
    snapshot = opening + run1[:5] + run2 + chapter
    missing = run1[5:]
    adapter, transport = _make_prepared_adapter()
    transport._script[("timeline", "set_current")] = [{"success": True}] * 9
    transport._script[("timeline", "get_track_count")] = [{"count": 3, "success": True}]
    transport._script[("timeline", "get_items_in_track")] = [
        _rows(snapshot),
        _rows(_expected_abs_spans()),
    ]
    transport._script[("timeline", "get_media_pool_item")] = [
        {"name": f"{TIMELINE_NAME}-telop-telop-persistent", "id": "mpi-existing"}
    ]
    transport._script[("media_pool", "append_to_timeline")] = [
        {"count": 6, "items": [], "success": True, "verification_status": "readback_verified"}
    ]
    transport._script[("fusion_comp", "get_text_plus")] = [
        {"tool_name": "Template", "input_name": "StyledText", "text": "スタブテロップ"}
    ] * 3
    transport._script[("fusion_comp", "get_input")] = _GET_TRIO * 3

    adapter("telop_generation_probe", "apply_telop", _telop_params())

    assert _actions(transport, "create_timeline") == []
    assert _actions(transport, "insert_fusion_title") == []
    assert _actions(transport, "safe_set_inputs") == []
    appends = _actions(transport, "append_to_timeline")
    assert len(appends) == 1
    clips = _clip_infos(appends[0])
    assert [clip["record_frame"] for clip in clips] == [span[0] for span in missing]
    assert clips[0]["clip_id"] == "mpi-existing"


def test_telop_drifted_card_text_is_a_typed_failure(stub_binding: None) -> None:
    # Given: all spans present but a card's nested text drifted
    # When: the same mutation runs again
    # Then: typed telop-text-mismatch — never a silent overwrite
    adapter, transport = _make_prepared_adapter()
    transport._script[("timeline", "set_current")] = [{"success": True}] * 3
    transport._script[("timeline", "get_track_count")] = [{"count": 3, "success": True}]
    transport._script[("timeline", "get_items_in_track")] = [
        _rows(_expected_abs_spans()),
    ]
    transport._script[("fusion_comp", "get_text_plus")] = [
        {"tool_name": "Template", "input_name": "StyledText", "text": "drifted テロップ"}
    ]
    transport._script[("fusion_comp", "get_input")] = list(_GET_TRIO)

    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("telop_generation_probe", "apply_telop", _telop_params())
    assert excinfo.value.code == "telop-text-mismatch"


def test_telop_final_scan_gap_or_missing_span_is_a_typed_failure(
    stub_binding: None,
) -> None:
    # Given: a final scan missing one expected tile
    # When: the telop mutation runs
    # Then: typed telop-span-mismatch
    adapter, transport = _make_prepared_adapter()
    _script_creation(transport)
    dropped = _expected_abs_spans()[:-2]
    transport._script[("timeline", "get_items_in_track")] = [
        _rows([]),
        _rows(dropped),
    ]
    with pytest.raises(LiveAdapterError) as missing:
        adapter("telop_generation_probe", "apply_telop", _telop_params())
    assert missing.value.code == "telop-span-mismatch"

    # Given: a final scan whose rows overlap (same-track overlap refusal)
    adapter2, transport2 = _make_prepared_adapter()
    _script_creation(transport2)
    overlapped = [
        *_expected_abs_spans(),
        (TIMELINE_START + CHAPTER_GAP[0] + 10, TIMELINE_START + CHAPTER_GAP[1] + 10),
    ]
    transport2._script[("timeline", "get_items_in_track")] = [
        _rows([]),
        _rows(overlapped),
    ]
    with pytest.raises(LiveAdapterError) as overlap:
        adapter2("telop_generation_probe", "apply_telop", _telop_params())
    assert overlap.value.code == "telop-track-overlap"


def test_telop_single_card_longer_than_the_card_media_refuses_typed(
    stub_binding: None,
) -> None:
    # Given: an opening span (200f) longer than the measured card length
    # When: the telop mutation runs
    # Then: typed refusal — one card cannot cover more than its media
    adapter, _transport = _make_prepared_adapter()
    params = _telop_params()
    cards = cast("list[dict[str, object]]", params["cards"])
    cards[0]["record_span"] = {"start_frame": 0, "end_frame": 200}
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("telop_generation_probe", "apply_telop", params)
    assert excinfo.value.code == "telop-span-exceeds-card-length"


def test_telop_unknown_style_profile_refuses_typed() -> None:
    # Given: a style reference outside the profile's telop recipe
    # When: the telop mutation runs
    # Then: typed refusal before any vendor call
    adapter, transport = _make_prepared_adapter()
    params = {**_telop_params(), "style_profile_id": "telop/unknown"}
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("telop_generation_probe", "apply_telop", params)
    assert excinfo.value.code == "telop-style-profile-unsupported"
    assert transport.calls == []


def test_telop_requires_prepared_timeline(stub_binding: None) -> None:
    # Given: no prepared session state
    # When: applying telop on a fresh adapter
    # Then: typed timeline-not-prepared
    adapter = LiveMcpAdapter(ScriptedTransport({}), media_paths={})
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("telop_generation_probe", "apply_telop", _telop_params())
    assert excinfo.value.code == "timeline-not-prepared"
