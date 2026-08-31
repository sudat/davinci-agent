# noqa: INP001 (evidence tree is not an importable package by design)
"""Pure rules and closed label sets for the resolve auto-caption probe.

Stdlib only — no services imports — so selfchecks never load the live
client stack. Owns: the verdict/allowlist label sets, subtitle item
validation + canonicalization (exact text, integer half-open
``[start, end)`` frames, no overlap), settings-candidate classification
from NON-generating echo responses, the committed-output string allowlist
(counts / sha256 / closed labels only), dual-run stability, and
deterministic ``HH:MM:SS:FF`` duration parsing.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from itertools import pairwise
from typing import Final, TypeGuard

HEX64_RE: Final = re.compile(r"^[0-9a-f]{64}$")
HEX40_RE: Final = re.compile(r"^[0-9a-f]{40}$")
#: Numeric-shaped strings: versions ("21.0.4.5"), settings echo keys on the
#: measured Resolve 21 build ("0.0"), frame counts.
NUMERIC_RE: Final = re.compile(r"^[0-9]+([.][0-9]+)*$")
#: Field names of this probe's own JSON schemas (snake_case identifiers).
FIELD_KEY_RE: Final = re.compile(r"^[a-z][a-z0-9_]*$")
#: Schema-version identifiers this probe emits (repo idiom ``*-v<N>``).
SCHEMA_RE: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*-v[0-9]+$")

#: One validated subtitle cue: exact ``(text, start, end)``; the span is in
#: INTEGER ABSOLUTE record frames — Task 2 must subtract the recorded
#: timeline start before any milliseconds conversion.
type Cue = tuple[str, int, int]

#: Language setting values in frozen candidate order (plan Todo 1): "ja"
#: first, then the documented Japanese constant the pinned MCP maps to
#: resolve.AUTO_CAPTION_JAPANESE.
LANGUAGE_CANDIDATES: Final = ("ja", "japanese")

#: ``HH:MM:SS:FF`` timecode part count.
TIMECODE_PARTS: Final = 4

#: Closed enumeration of verdict / refusal reason labels — the only
#: non-numeric, non-hash, non-name strings allowed in committed outputs.
VERDICT_LABELS: Final = frozenset({
    "generated-and-read", "generation-failed", "readback-empty",
    "readback-invalid", "settings-rejected", "media-missing",
    "media-record-ambiguous", "media-mismatch", "project-exists",
    "run-already-recorded", "timeout", "interrupted", "error",
    "capability-proven", "capability-unproven",
})

#: Exact-match strings allowed in committed outputs beyond labels/hashes:
#: language values, pin/server modes, run/project/timeline names, provider
#: id, and flow-phase names that appear in failure records.
_ALLOWED_EXACT: Final = frozenset({
    *LANGUAGE_CANDIDATES, "auto", "language", "compound", "r1", "r2",
    "probe-resolve-autocap-r1", "probe-resolve-autocap-r2", "probe-autocap-tl",
    "resolve-auto-caption", "media_verify", "project_exists_check",
    "project_create", "project_fps", "timeline_create", "timeline_current",
    "tracks_before", "import", "clip_props", "append_audio", "settings_echo",
    "generate", "readback", "flow", "exception", "stable-and-clean", "cleanup",
})

#: Timeout classification is by exception type name: the live stack raises
#: services.mcp_client.errors.McpTimeoutError; the selfcheck fabricates a
#: class with that name (this module must not import services).
TIMEOUT_TYPE_NAME: Final = "McpTimeoutError"


class ProbeRefusalError(Exception):
    """Typed refusal: closed-enumeration reason label + counts-only detail."""

    def __init__(self, reason: str, detail: str = "") -> None:
        if reason not in VERDICT_LABELS:
            msg = f"unknown refusal reason label: {reason!r}"
            raise ValueError(msg)
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _is_frame_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def canonical_cues(rows: Sequence[object]) -> tuple[list[Cue], str]:
    """Validate subtitle item rows; return ``(cues, canonical sha256)``.

    Each row must be a mapping with non-empty string ``text`` and integer
    ``start``/``end`` as a half-open ``[start, end)`` span (``end > start``);
    ordered by ``(start, end)``, no span may overlap its predecessor. The
    canonical hash is sha256 over compact sorted-keys JSON of the exact
    ``(text, start, end)`` list — text is byte-exact, never normalized.
    """
    cues: list[Cue] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ProbeRefusalError("readback-invalid", f"row {index} not an object")
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ProbeRefusalError("readback-invalid", f"row {index} text empty")
        start, end = row.get("start"), row.get("end")
        if not _is_frame_int(start) or not _is_frame_int(end):
            raise ProbeRefusalError("readback-invalid", f"row {index} frames not integer")
        if end <= start:
            raise ProbeRefusalError("readback-invalid",
                                    f"row {index} start={start} end={end}")
        cues.append((text, start, end))
    cues.sort(key=lambda cue: (cue[1], cue[2]))
    for prev, cur in pairwise(cues):
        if cur[1] < prev[2]:
            raise ProbeRefusalError("readback-invalid",
                                    f"rows overlap at start={cur[1]}")
    canonical = json.dumps(
        [{"end": end, "start": start, "text": text} for text, start, end in cues],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return cues, hashlib.sha256(canonical).hexdigest()


def cues_json(cues: Sequence[Cue]) -> list[dict[str, object]]:
    """The private-readback JSON shape for a validated cue list."""
    return [{"text": text, "start": start, "end": end}
            for text, start, end in cues]


def classify_candidate(echo: dict[str, object]) -> str:
    """Classify one language candidate from a NON-generating echo response.

    The echo is only usable when it carries the measured successful shape —
    ``success`` true, ``would_generate`` true, no error envelope. A failed
    echo (explicit ``success=false`` or an error envelope) is
    ``echo-invalid`` and can never be ``accepted`` even if a settings dict
    is present. Otherwise ``accepted`` only when the language key survived
    into a non-empty normalized settings echo (dropped keys are reported in
    ``ignored_settings``).
    """
    if (isinstance(echo.get("error"), dict)
            or echo.get("success") is not True
            or echo.get("would_generate") is not True):
        return "echo-invalid"
    ignored = echo.get("ignored_settings")
    if isinstance(ignored, list) and "language" in [str(k) for k in ignored]:
        return "keys-ignored"
    settings = echo.get("settings")
    if not isinstance(settings, dict) or not settings:
        return "keys-ignored"
    return "accepted"


def string_allowed(value: str) -> bool:
    """Committed-output allowlist for one string value."""
    return (
        value in _ALLOWED_EXACT
        or value in VERDICT_LABELS
        or HEX64_RE.fullmatch(value) is not None
        or HEX40_RE.fullmatch(value) is not None
        or NUMERIC_RE.fullmatch(value) is not None
        or SCHEMA_RE.fullmatch(value) is not None
    )


def assert_sanitized(value: object, path: str = "$") -> None:
    """Recursively refuse any string outside the allowlist (values AND keys)."""
    if isinstance(value, str):
        if not string_allowed(value):
            raise ProbeRefusalError("error",
                                    f"unsanitized string at {path} len={len(value)}")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not (
                FIELD_KEY_RE.fullmatch(key) or string_allowed(key)
            ):
                raise ProbeRefusalError("error", f"unsanitized key at {path}")
            assert_sanitized(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_sanitized(item, f"{path}[{index}]")
    elif value is not None and not isinstance(value, bool | int | float):
        raise ProbeRefusalError("error", f"unsanitized type at {path}")


def stability_verdict(
    cues_r1: Sequence[Cue], sha_r1: str,
    cues_r2: Sequence[Cue], sha_r2: str,
) -> dict[str, object]:
    """Exact-list stability between the two runs (private data, public shape)."""
    return {
        "stable": list(cues_r1) == list(cues_r2) and sha_r1 == sha_r2,
        "r1_sha256": sha_r1,
        "r2_sha256": sha_r2,
        "r1_item_count": len(cues_r1),
        "r2_item_count": len(cues_r2),
    }


def parse_duration_frames(duration: object, fps: object) -> int:
    """Deterministic ``HH:MM:SS:FF`` @ fps -> total frames (typed refusal).

    ``fps`` may be the string form (``"30"``) or Resolve's native float
    (``30.0`` — measured live on 21.0.4.5 via ``get_clip_property``).
    """
    if not isinstance(duration, str):
        raise ProbeRefusalError("error", "clip duration not a string")
    if isinstance(fps, bool) or not isinstance(fps, str | int | float):
        raise ProbeRefusalError("error", "clip fps not a number")
    try:
        rate = float(fps)
    except ValueError as exc:
        raise ProbeRefusalError("error", "clip fps unparseable") from exc
    parts = duration.split(":")
    if len(parts) != TIMECODE_PARTS or rate <= 0:
        raise ProbeRefusalError("error", "clip duration shape invalid")
    try:
        hh, mm, ss, ff = (int(part) for part in parts)
    except ValueError as exc:
        raise ProbeRefusalError("error", "clip duration unparseable") from exc
    total = ff + rate * (ss + 60 * mm + 3600 * hh)
    if total <= 0:
        raise ProbeRefusalError("error", "clip duration non-positive")
    return round(total)


__all__ = [
    "LANGUAGE_CANDIDATES",
    "TIMEOUT_TYPE_NAME",
    "VERDICT_LABELS",
    "ProbeRefusalError",
    "assert_sanitized",
    "canonical_cues",
    "classify_candidate",
    "cues_json",
    "parse_duration_frames",
    "stability_verdict",
    "string_allowed",
]
