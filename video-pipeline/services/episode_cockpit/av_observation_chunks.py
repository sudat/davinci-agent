"""Per-chunk observation assembly for the chunked Gemini AV route.

One runner pass per core: cache lookup or ONE Gemini attempt, the
adopted GLOBAL events as one core-bounded chunk observation, meter
entries and timestamp drifts accumulated; the shared journal record then
reports which cores are gemini-ok, glm-backfilled, or still insufficient.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from services.episode_cockpit.av_chunk_media import _extract_av_chunk
from services.episode_cockpit.av_observation_backfill import _backfilled, _ChunkBackfill
from services.media_intelligence.gemini_av_record import cached_chunk_outcome
from services.media_intelligence.gemini_av_wire import observe_chunk_av
from services.media_intelligence.sample_observation import (
    SampleCandidate,
    SampleChunk,
    SampleChunkObservation,
    SampleObservationError,
    observation_record,
    pick_candidate_anchors,
)

if TYPE_CHECKING:
    from services.editorial_v2.editorial_pins import EditorialPinV2
    from services.media_intelligence.gemini_av_files import GeminiAvHttp
    from services.media_intelligence.gemini_av_models import AvChunk, AvChunkOutcome


def _chunk_observation_from_outcome(
    outcome: AvChunkOutcome, core: SampleChunk
) -> SampleChunkObservation:
    """Adopted GLOBAL events as ONE core-bounded chunk observation.

    Candidates are the adopted events carrying a reason, clamped into the
    core (an adopted start is always in-core; only the end can poke into
    the next core's context). The shared pick/widen machinery runs
    unchanged downstream.
    """

    findings = tuple(
        event.visual.strip() or "(no visual description)" for event in outcome.events
    ) or ("(no adopted core events)",)
    candidates: list[SampleCandidate] = []
    for event in outcome.events:
        reason = event.sample_candidate_reason.strip()
        if not reason:
            continue
        start = max(event.start_seconds, core.start_seconds)
        end = min(event.end_seconds, core.end_seconds)
        if end < start:
            continue
        candidates.append(
            SampleCandidate(start_second=start, end_second=end, reason=reason)
        )
    return SampleChunkObservation(
        chunk_index=core.index,
        chunk_start_seconds=core.start_seconds,
        chunk_end_seconds=core.end_seconds,
        findings=findings,
        candidates=tuple(candidates),
        uncertainty=outcome.uncertain_flags or None,
        quality_insufficient=outcome.chunk_insufficient,
        insufficiency_note=None,
    )


@dataclass
class _AvChunkRunner:
    """One Gemini attempt per core, accumulating observations + provenance.

    A failed/insufficient chunk is backfilled by the GLM silent path plus
    the ASR slice (never a Gemini retry); the journal then records which
    cores are gemini-ok, glm-backfilled, or still insufficient.
    """

    ffmpeg: Path
    proxy: Path
    chunk_workspace: Path
    transport: GeminiAvHttp
    pin: EditorialPinV2
    env: dict[str, str]
    episode_dir: Path
    episode_id: str
    proxy_sha: str
    backfill: _ChunkBackfill
    observations: list[SampleChunkObservation] = field(default_factory=list)
    provenance: list[dict[str, object]] = field(default_factory=list)
    meters: list[dict[str, object]] = field(default_factory=list)
    drifts: list[dict[str, object]] = field(default_factory=list)

    def run(self, core: SampleChunk, chunk: AvChunk) -> None:
        try:
            outcome = cached_chunk_outcome(
                episode_dir=self.episode_dir,
                proxy_sha256=self.proxy_sha,
                pin=self.pin,
                chunk=chunk,
            )
            if outcome is None:
                clip = _extract_av_chunk(
                    ffmpeg=self.ffmpeg,
                    proxy=self.proxy,
                    chunk=chunk,
                    workspace_dir=self.chunk_workspace,
                )
                outcome = observe_chunk_av(
                    self.transport,
                    pin=self.pin,
                    env=self.env,
                    episode_dir=self.episode_dir,
                    episode_id=self.episode_id,
                    proxy_sha256=self.proxy_sha,
                    chunk=chunk,
                    clip_path=clip,
                )
        except SampleObservationError as error:
            self.observations.append(
                _backfilled(core, self.backfill, error.code, self.provenance)
            )
            return
        self.meters.append(_meter_entry(chunk.index, outcome))
        self.drifts.extend(
            {
                "chunk_index": core.index,
                "index": flag.index,
                "raw_start": flag.raw_start,
                "raw_end": flag.raw_end,
            }
            for flag in outcome.drift_flags
        )
        if outcome.chunk_insufficient or not outcome.events:
            note = (
                f"{len(outcome.drift_flags)} clip-local event(s) out of range "
                "(no clamp, no rescue)"
                if outcome.chunk_insufficient
                else "no adopted core events"
            )
            self.observations.append(
                _backfilled(core, self.backfill, note, self.provenance)
            )
            return
        self.observations.append(_chunk_observation_from_outcome(outcome, core))
        self.provenance.append(
            {
                "index": core.index,
                "core_start_seconds": core.start_seconds,
                "core_end_seconds": core.end_seconds,
                "status": "gemini-ok",
                "detail": (
                    f"adopted {len(outcome.events)} core event(s); "
                    f"reused={outcome.reused}"
                ),
            }
        )


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _meter_entry(index: int, outcome: AvChunkOutcome) -> dict[str, object]:
    meter = outcome.meter
    cost = meter.get("cost_usd")
    cost_map = cost if isinstance(cost, dict) else {}
    total = cost_map.get("total_usd")
    if not isinstance(total, (int, float)):
        total = float(cost_map.get("input_usd_upper", cost_map.get("input_usd", 0.0))) + float(
            cost_map.get("output_usd", 0.0)
        )
    tokens = meter.get("tokens")
    return {
        "index": index,
        "model": meter.get("model"),
        "latency_ms": meter.get("latency_ms"),
        "tokens": tokens if isinstance(tokens, dict) else {},
        "cost_usd": cost_map,
        "total_usd": round(float(total), 6),
        "reused": outcome.reused,
    }


def _av_journal_record(
    *,
    cores: tuple[SampleChunk, ...],
    runner: _AvChunkRunner,
    policy_text: str,
) -> dict[str, object]:
    """Shared journal plus additive per-chunk AV provenance (no new artifact).

    ``av_chunk_provenance`` says per core whether Gemini, the GLM/ASR
    backfill, or neither produced the observation; each chunk entry
    carries ``av_status`` / ``av_speech_source`` / ``av_audio_unconfirmed``
    (ambient/music/audiovisual_relation stay UNCONFIRMED off the Gemini
    path). ``meter`` sums the metered Gemini chunk calls.
    """

    observations, provenance, meters, drifts = (
        runner.observations,
        runner.provenance,
        runner.meters,
        runner.drifts,
    )
    anchors = pick_candidate_anchors(observations)
    record = observation_record(cores, observations, anchors)
    record["route"] = "audiovisual"
    record["timestamp_drift"] = drifts
    status_by_index = {entry["index"]: entry["status"] for entry in provenance}
    raw_chunks = record.get("chunks")
    journal_entries = raw_chunks if isinstance(raw_chunks, list) else []
    for entry in journal_entries:
        if not isinstance(entry, dict):
            continue
        status = status_by_index.get(entry["index"], "gemini-insufficient")
        entry["av_status"] = status
        if status == "gemini-ok":
            entry["av_speech_source"] = "gemini"
        elif status == "glm-backfilled":
            entry["av_speech_source"] = "asr-slice"
        else:
            entry["av_speech_source"] = "none"
        entry["av_audio_unconfirmed"] = status != "gemini-ok"
    ok = [entry["index"] for entry in provenance if entry["status"] == "gemini-ok"]
    backfilled = [
        entry["index"] for entry in provenance if entry["status"] == "glm-backfilled"
    ]
    record["audio_integration"] = "confirmed" if len(ok) == len(cores) else "partial"
    record["av_chunk_provenance"] = provenance
    record["av_chunks_gemini_ok"] = ok
    record["av_chunks_glm_backfilled"] = backfilled
    record["av_chunks_insufficient"] = [
        entry["index"] for entry in provenance if entry["status"] == "gemini-insufficient"
    ]
    meter: dict[str, object] = {
        "chunks": meters,
        "total_usd": round(sum(_number(entry.get("total_usd")) for entry in meters), 6),
        "cost_method": "sum of per-chunk Gemini calls (GLM backfill is not metered here)",
        "result_reused": all(entry["reused"] for entry in meters)
        and len(meters) == len(cores),
    }
    record["meter"] = meter
    record["result_reused"] = meter["result_reused"]
    record["policy_text"] = policy_text  # comparison CONDITIONS only, never commands
    return record
