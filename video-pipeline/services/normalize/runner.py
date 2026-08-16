"""``normalize_one``: one immutable original in, one committed NormalizeRecord out.

Order of operations is the contract: verify pinned binaries against the lock
(sha256, absolute paths) -> verify the source still hashes to its manifest ->
read the recipe from the lock -> derive the drop/dup prediction from the
INPUT's decode-level facts via ``services.conform`` -> guard the argv (network
refusal, absolute IO, never over the original) -> run pinned ffmpeg with a
bounded 600s timeout -> re-hash the source (immutability) -> verify the output
(decodability, exact CFR rate, 48 kHz, rotation/color policy, frame count
EXACTLY equal to the prediction) -> seal and atomically commit the record.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.contracts.primitives import RationalFrameRate
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.ingest.probe import ProbeExecutionError
from services.normalize.accounting import (
    SourceSpan,
    expected_conversion,
    source_span_from_probe,
)
from services.normalize.commit import VerifiedRun, build_normalize_record
from services.normalize.errors import (
    NormalizeExecutionError,
    NormalizeRecipeError,
    NormalizeVerificationError,
)
from services.normalize.models import NormalizeRecord, seal_normalize_record
from services.normalize.probe import decoded_video_sha256, probe_media_facts
from services.normalize.toolchain_guard import (
    build_argv,
    load_phase0b_lock,
    lookup_recipe,
    recipe_args_sha256,
    verify_pinned_binary,
)
from services.normalize.verify import OutputExpectation, verify_output

if TYPE_CHECKING:
    from services.conform.rate_model import CfrConversionReport
    from services.ingest.models import SourceManifest
    from services.normalize.probe import MediaFacts
    from services.toolchain.models import Phase0BToolchainLock
    from services.toolchain.normalization import NormalizeRecipe

FFMPEG_TIMEOUT_SECONDS: Final = 600
TARGET_PROFILE: Final = "cfr30"


@dataclass(frozen=True, slots=True)
class NormalizeContext:
    """Pinned-tool and IO destinations for one normalization run."""

    lock_path: Path
    ffmpeg: Path
    ffprobe: Path
    output_dir: Path
    record_out: Path


def _target_rate(lock: Phase0BToolchainLock) -> RationalFrameRate:
    target = lock.normalization.target.frame_rate
    return RationalFrameRate(num=target.num, den=target.den)


def _run_ffmpeg(argv: tuple[str, ...]) -> None:
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise NormalizeExecutionError(
            f"ffmpeg exceeded the bounded {FFMPEG_TIMEOUT_SECONDS}s timeout"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.strip() or "ffmpeg exited without success"
        raise NormalizeExecutionError(detail)


def _immutable_source_hash(source: Path, manifest: SourceManifest) -> str:
    if not source.is_file():
        raise NormalizeVerificationError(
            "stale_source_manifest", f"original is missing: {source}"
        )
    digest = sha256_file(source)
    if digest != manifest.file.sha256:
        raise NormalizeVerificationError(
            "stale_source_manifest",
            f"original {source} no longer hashes to its registered manifest",
        )
    return digest


def _probe_or_block(ffprobe: Path, media: Path, role: str) -> MediaFacts:
    try:
        return probe_media_facts(ffprobe, media)
    except ProbeExecutionError as error:
        raise NormalizeVerificationError(
            "undecodable_output", f"{role} is not decodable: {error}"
        ) from error


def _span_from(input_facts: MediaFacts) -> SourceSpan:
    try:
        return source_span_from_probe(input_facts.video)
    except ValueError as error:
        raise NormalizeVerificationError(
            "undecodable_output", f"input span is not exactly rational: {error}"
        ) from error


def _prediction(span: SourceSpan, lock: Phase0BToolchainLock) -> CfrConversionReport:
    return expected_conversion(span, _target_rate(lock))


def _recipe_for(lock: Phase0BToolchainLock, recipe_id: str) -> NormalizeRecipe:
    return lookup_recipe(lock.normalization, recipe_id)


def normalize_one(
    source_manifest: SourceManifest,
    target_profile: str,
    context: NormalizeContext,
) -> NormalizeRecord:
    """Produce one CFR Edit Mezzanine and commit its NormalizeRecord."""

    if target_profile != TARGET_PROFILE:
        raise NormalizeRecipeError(
            f"target profile {target_profile!r} is not frozen in the 0B lock"
        )
    lock = load_phase0b_lock(context.lock_path)
    lock_sha256 = sha256_file(context.lock_path)
    verify_pinned_binary("ffmpeg", context.ffmpeg, lock.ffmpeg.ffmpeg.sha256)
    verify_pinned_binary("ffprobe", context.ffprobe, lock.ffmpeg.ffprobe.sha256)

    source = Path(source_manifest.file.path)
    hash_before = _immutable_source_hash(source, source_manifest)
    recipe = _recipe_for(lock, source_manifest.edit_source_recipe.recipe_id)
    if recipe_args_sha256(recipe) != source_manifest.edit_source_recipe.args_sha256:
        raise NormalizeRecipeError(
            "locked recipe argv no longer matches the source manifest pointer"
        )

    input_facts = _probe_or_block(context.ffprobe, source, "input")
    prediction = _prediction(_span_from(input_facts), lock)

    output = context.output_dir / f"{source.stem}.edit-source.mov"
    argv = build_argv(
        recipe.argv, ffmpeg=context.ffmpeg, source=source, output=output
    )
    context.output_dir.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(argv)

    if sha256_file(source) != hash_before:
        raise NormalizeVerificationError(
            "source_mutated", "original changed during normalization"
        )
    output_facts = _probe_or_block(context.ffprobe, output, "output")
    verify_output(
        output_facts,
        OutputExpectation(
            target=lock.normalization.target,
            source=input_facts,
            expected_output_frames=prediction.output_frames,
        ),
    )

    sealed = seal_normalize_record(
        build_normalize_record(
            VerifiedRun(
                source_manifest=source_manifest,
                source=source,
                source_sha256=hash_before,
                output=output,
                output_sha256=sha256_file(output),
                argv=argv,
                lock=lock,
                lock_sha256=lock_sha256,
                prediction=prediction,
                output_facts=output_facts,
                decoded_video_sha256=decoded_video_sha256(context.ffmpeg, output),
            )
        )
    )
    atomic_write(context.record_out, canonical_model_bytes(sealed))
    return sealed
