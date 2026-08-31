# noqa: INP001 (evidence tree is not an importable package by design)
"""Scripted fakes for the auto-caption probe selfcheck (no Resolve).

``FakeCall`` answers raw ``(tool, action, params)`` triples from a scripted
table (values may be per-params callables, matching vendor shapes measured
live on Resolve 21.0.4.5 / MCP 2.98.3 — including the FLOAT ``FPS`` and the
``{"0.0": 0.0}`` settings echo). ``FakeSession`` wraps a FakeCall with the
``DisposableSession`` shape: ``close`` is recorded into the same call trace
so the disposal-ordering proof can assert close-after-cleanup.
"""

from __future__ import annotations

from collections.abc import Callable

from probe_seam import GENERATE_TIMEOUT_S, TIMELINE_NAME

FAKE_ITEM_COUNT = 2
RaiseIf = Callable[[str, str, dict[str, object]], bool]


class FakeCall:
    """Scripted ``(tool, action, params)`` responses for the selfcheck."""

    def __init__(self, script: dict[tuple[str, str], object], *,
                 raise_if: RaiseIf | None = None,
                 exc: BaseException | None = None,
                 existing_project: bool = False) -> None:
        self.script = script
        self.raise_if = raise_if
        self.exc = exc
        self.existing_project = existing_project
        self.calls: list[tuple[str, str]] = []
        self.deleted = False

    def __call__(self, tool: str, action: str, params: dict[str, object],
                 timeout_seconds: float = GENERATE_TIMEOUT_S,  # noqa: ARG002
                 ) -> dict[str, object]:
        self.calls.append((tool, action))
        if self.raise_if is not None and self.raise_if(tool, action, params):
            if self.exc is None:
                raise TypeError("raise_if matched without exc")
            raise self.exc
        if (tool, action) == ("project_manager", "delete"):
            self.deleted = True
            return {"success": True}
        if (tool, action) == ("project_manager", "load"):
            if self.existing_project:
                return {"success": True}  # a same-named project already exists
            return {"error": {"message": "not found", "code": -1}}
        response = self.script[(tool, action)]
        if callable(response):
            response = response(params)
        if isinstance(response, BaseException):
            raise response
        if not isinstance(response, dict):
            raise TypeError("scripted response not a dict")
        return response


class FakeSession:
    """DisposableSession over a FakeCall; close order is provable."""

    def __init__(self, fake: FakeCall) -> None:
        self.fake = fake
        self.closed = False

    @property
    def call(self) -> FakeCall:
        return self.fake

    def close(self) -> None:
        self.closed = True
        self.fake.calls.append(("<session>", "close"))


def fake_script(item_count: int = FAKE_ITEM_COUNT) -> dict[tuple[str, str], object]:
    """The full happy-path vendor script (measured shapes, prose fake)."""
    subtitle_items = [
        {"name": f"synth-cue-{i}", "start": 10 * (i + 1), "end": 10 * (i + 1) + 5,
         "id": f"uuid-{i}", "duration": 5} for i in range(item_count)
    ]
    audio_item = {"name": "edit-source.mov", "start": 3600, "end": 12067,
                  "id": "a1", "duration": 8467}
    cues = [{"text": str(it["name"]), "start": it["start"], "end": it["end"]}
            for it in subtitle_items]

    def items_by_track(params: dict[str, object]) -> dict[str, object]:
        track = str(params.get("track_type", ""))
        return {"items": [audio_item] if track == "audio" else subtitle_items}

    return {
        ("project_manager", "create"): {"success": True,
                                        "name": "probe-resolve-autocap-r1"},
        ("project_settings", "set_setting"): {"success": True},
        ("media_pool", "create_timeline"): {"success": True, "name": TIMELINE_NAME},
        ("timeline", "set_current"): {"success": True},
        ("timeline", "get_current"): {"success": True, "start_frame": 3600,
                                      "name": TIMELINE_NAME},
        ("timeline", "get_track_count"): {"success": True, "count": 0},
        ("timeline", "get_items_in_track"): items_by_track,
        ("media_pool", "safe_import_media"): {
            "success": True, "imported": 1,
            "clips": [{"name": "edit-source.mov", "id": "clip-1"}]},
        ("media_pool_item", "get_clip_property"): {
            "success": True,
            "properties": {"FPS": 30.0, "Duration": "00:04:42:07"}},
        ("media_pool", "append_to_timeline"): {"success": True, "count": 1},
        ("timeline", "subtitle_generation_probe"): {
            "success": True, "would_generate": True, "verified": True,
            "subtitle_tracks_before": 0, "subtitle_tracks_after": 1,
            "settings": {"0.0": 0.0}, "ignored_settings": []},
        ("timeline", "get_transcript"): {"cue_count": item_count,
                                         "has_subtitles": True,
                                         "cues": cues, "text": "joined"},
    }


def with_generation(script: dict[tuple[str, str], object],
                    generation: dict[str, object],
                    ) -> dict[tuple[str, str], object]:
    """Split the probe action into echo vs generation arms by params.

    The happy echo (non-generating shape) stays for the settings
    classification; only the ``allow_generate=True`` call answers with
    ``generation`` — used for vendor-failure scenarios with stale rows.
    """
    echo = script[("timeline", "subtitle_generation_probe")]

    def respond(params: dict[str, object]) -> dict[str, object]:
        if params.get("allow_generate") is True:
            return generation
        if callable(echo):
            echoed = echo(params)
            return echoed if isinstance(echoed, dict) else {}
        return echo if isinstance(echo, dict) else {}

    script[("timeline", "subtitle_generation_probe")] = respond
    return script


__all__ = ["FAKE_ITEM_COUNT", "FakeCall", "FakeSession", "fake_script",
           "with_generation"]
