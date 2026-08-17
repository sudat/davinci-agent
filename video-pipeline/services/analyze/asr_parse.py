"""Parse whisper-cli JSON output into the TranscriptArtifact contract.

The pinned whisper-cli (commit 1fe009ca) writes per-segment ``offsets``
values already in integer milliseconds and, with ``--output-json-full``,
per-token timestamps under ``tokens``. Float fields emitted by the CLI
(``p``, ``t_dtw``) are never read here; they survive only inside the raw
evidence file. Token timing is parsed exclusively as experimental evidence
and cross-checked against the canonical segment spans.
"""

from __future__ import annotations

import json
from typing import Final

from pydantic import ValidationError

from services.analyze.asr_models import (
    TRANSCRIPT_PRODUCER,
    AsrInputBinding,
    AsrParseError,
    AsrSettingsEcho,
    ExperimentalTiming,
    ExperimentalToken,
    SegmentTokenTiming,
    TranscriptArtifact,
    TranscriptSegment,
    transcript_content_hash,
)
from services.contracts.primitives import ArtifactRef

EXPERIMENTAL_TOLERANCE_MS: Final = 1000


def _require_int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AsrParseError(f"{where} must be an integer, got {type(value).__name__}")
    return value


def _parse_tokens(raw: object, index: int) -> tuple[ExperimentalToken, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise AsrParseError(f"transcription[{index}].tokens is not a list")
    tokens: list[ExperimentalToken] = []
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AsrParseError(f"transcription[{index}].tokens[{position}] is not an object")
        text = item.get("text")
        offsets = item.get("offsets")
        # per-token timestamps are optional in the CLI output; tokens without
        # usable offsets are skipped (experimental evidence, never canonical)
        if not isinstance(text, str) or not isinstance(offsets, dict):
            continue
        if "from" not in offsets or "to" not in offsets:
            continue
        tokens.append(
            ExperimentalToken(
                text=text,
                start_ms=_require_int(
                    offsets["from"], f"transcription[{index}].tokens[{position}].offsets.from"
                ),
                end_ms=_require_int(
                    offsets["to"], f"transcription[{index}].tokens[{position}].offsets.to"
                ),
            )
        )
    return tuple(tokens)


def parse_whisper_payload(
    raw: bytes,
) -> tuple[tuple[TranscriptSegment, ...], tuple[SegmentTokenTiming, ...]]:
    try:
        payload: object = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AsrParseError(f"whisper-cli output is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise AsrParseError("whisper-cli output is not a JSON object")
    transcription = payload.get("transcription")
    if not isinstance(transcription, list) or not transcription:
        raise AsrParseError("whisper-cli output has no non-empty transcription list")
    segments: list[TranscriptSegment] = []
    token_groups: list[SegmentTokenTiming] = []
    for index, entry in enumerate(transcription):
        if not isinstance(entry, dict):
            raise AsrParseError(f"transcription[{index}] is not an object")
        text = entry.get("text")
        if not isinstance(text, str):
            raise AsrParseError(f"transcription[{index}].text is not a string")
        offsets = entry.get("offsets")
        if not isinstance(offsets, dict):
            raise AsrParseError(f"transcription[{index}].offsets is missing")
        segments.append(
            TranscriptSegment(
                start_ms=_require_int(offsets.get("from"), f"transcription[{index}].offsets.from"),
                end_ms=_require_int(offsets.get("to"), f"transcription[{index}].offsets.to"),
                text=text,
            )
        )
        tokens = _parse_tokens(entry.get("tokens"), index)
        token_groups.append(SegmentTokenTiming(segment_index=index, tokens=tokens))
    return tuple(segments), tuple(token_groups)


def check_experimental_consistency(
    token_groups: tuple[SegmentTokenTiming, ...], segments: tuple[TranscriptSegment, ...]
) -> str | None:
    if len(token_groups) != len(segments):
        return (
            f"token group count {len(token_groups)} differs from segment count {len(segments)}"
        )
    for group, segment in zip(token_groups, segments, strict=True):
        for token in group.tokens:
            too_early = token.start_ms < segment.start_ms - EXPERIMENTAL_TOLERANCE_MS
            too_late = token.end_ms > segment.end_ms + EXPERIMENTAL_TOLERANCE_MS
            if too_early or too_late:
                return (
                    f"segment {group.segment_index} token {token.text!r} span "
                    f"{token.start_ms}-{token.end_ms}ms deviates from segment bounds "
                    f"{segment.start_ms}-{segment.end_ms}ms beyond "
                    f"{EXPERIMENTAL_TOLERANCE_MS}ms tolerance"
                )
    return None


def build_transcript_artifact(
    *,
    binding: AsrInputBinding,
    segments: tuple[TranscriptSegment, ...],
    token_groups: tuple[SegmentTokenTiming, ...],
    settings_echo: AsrSettingsEcho,
) -> TranscriptArtifact:
    inconsistency = check_experimental_consistency(token_groups, segments)
    experimental = ExperimentalTiming(
        label="experimental-token-timing",
        token_level=token_groups if any(group.tokens for group in token_groups) else None,
        consistent_with_segments=inconsistency is None,
        inconsistency=inconsistency,
    )
    try:
        content_hash = transcript_content_hash(binding, segments, experimental, settings_echo)
        return TranscriptArtifact(
            artifact_id=f"transcript-{content_hash[:16]}",
            artifact_type="transcript_asr_whisper_cpp",
            schema_version="asr-transcript-v1",
            content_hash=content_hash,
            producer=TRANSCRIPT_PRODUCER,
            inputs=(
                ArtifactRef(artifact_id="asr-input-media", sha256=binding.media_sha256),
                ArtifactRef(artifact_id="asr-preprocessed-wav", sha256=binding.wav_sha256),
            ),
            input_binding=binding,
            segments=segments,
            experimental=experimental,
            settings_echo=settings_echo,
        )
    except ValidationError as error:
        raise AsrParseError(f"parsed transcript violates the artifact contract: {error}") from error
