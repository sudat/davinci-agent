"""Offline fault scenarios for normalize QA (``QA_FAULT_FIXTURE`` pattern).

Faults drive the real ``normalize_one`` flow through patched module seams
(the established ingest ``cli_faults`` pattern): a recipe variant that drops
the audio stream (silent metadata loss), an off-by-one accounting prediction
(unexplained frame count), an online-relink recipe (refused pre-execution),
and record-level output/source hash confusion (rejected by validation).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

import services.normalize.runner as runner_module
from services.normalize.cli_support import build_source_manifest
from services.normalize.errors import NormalizeError, NormalizeVerificationError
from services.normalize.models import NormalizeRecord
from services.normalize.runner import NormalizeContext, normalize_one
from services.normalize.toolchain_guard import load_normalization_section

if TYPE_CHECKING:
    from services.conform.rate_model import CfrConversionReport
    from services.contracts.primitives import RationalFrameRate
    from services.normalize.accounting import SourceSpan
    from services.toolchain.normalization import NormalizationSection, NormalizeRecipe


class FaultSpecError(Exception):
    """The fault fixture is not a valid fault specification."""


class FaultSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    fault: Literal[
        "metadata_loss_recipe",
        "frame_count_off_by_one",
        "online_relink_recipe",
        "output_source_hash_confusion",
    ]


def load_fault_spec(path: Path) -> FaultSpec:
    try:
        return FaultSpec.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as error:
        raise FaultSpecError(f"invalid fault fixture {path}: {error}") from error


@dataclass(frozen=True, slots=True)
class FaultRun:
    spec: FaultSpec
    arguments: argparse.Namespace

    def _normalize(self) -> int:
        manifest = build_source_manifest(self.arguments)
        try:
            normalize_one(
                manifest,
                self.arguments.target,
                NormalizeContext(
                    lock_path=self.arguments.lock,
                    ffmpeg=self.arguments.ffmpeg,
                    ffprobe=self.arguments.ffprobe,
                    output_dir=self.arguments.output_dir,
                    record_out=self.arguments.out,
                ),
            )
        except NormalizeError as error:
            reason = (
                error.reason_code
                if isinstance(error, NormalizeVerificationError)
                else error.label
            )
            print(f"reason={reason}")
            print(f"error={error.label}: {error}")
            return 2
        print("normalize: committed")
        return 0

    def run(self) -> int:
        handlers: dict[str, Callable[[], int]] = {
            "metadata_loss_recipe": self._metadata_loss_recipe,
            "frame_count_off_by_one": self._frame_count_off_by_one,
            "online_relink_recipe": self._online_relink_recipe,
            "output_source_hash_confusion": self._hash_confusion,
        }
        return handlers[self.spec.fault]()

    def _patched_lookup(
        self, mutate: Callable[[NormalizeRecipe], NormalizeRecipe]
    ) -> int:
        """Patch the recipe seam (and its pointer-hash cross-check) for one run.

        The cross-check keeps hashing the UNMUTATED locked recipe so the fault
        exercises execution/output verification rather than recipe lookup.
        """

        real_lookup = runner_module.lookup_recipe
        real_hash = runner_module.recipe_args_sha256
        lock_section = load_normalization_section(self.arguments.lock)

        def mutated(
            section: NormalizationSection, recipe_id: str
        ) -> NormalizeRecipe:
            return mutate(real_lookup(section, recipe_id))

        def unmutated_hash(recipe: NormalizeRecipe) -> str:
            return real_hash(real_lookup(lock_section, recipe.fixture_id))

        runner_module.lookup_recipe = mutated
        runner_module.recipe_args_sha256 = unmutated_hash
        try:
            return self._normalize()
        finally:
            runner_module.lookup_recipe = real_lookup
            runner_module.recipe_args_sha256 = real_hash

    def _metadata_loss_recipe(self) -> int:
        def drop_audio(recipe: NormalizeRecipe) -> NormalizeRecipe:
            argv = list(recipe.argv)
            index = argv.index("-c:a")
            argv[index : index + 2] = ["-an"]
            return recipe.model_copy(update={"argv": tuple(argv)})

        return self._patched_lookup(drop_audio)

    def _online_relink_recipe(self) -> int:
        def remote_input(recipe: NormalizeRecipe) -> NormalizeRecipe:
            argv = list(recipe.argv)
            argv[argv.index("{input}")] = "http://media.internal.example.com/source.mov"
            return recipe.model_copy(update={"argv": tuple(argv)})

        return self._patched_lookup(remote_input)

    def _frame_count_off_by_one(self) -> int:
        real = runner_module.expected_conversion

        def off_by_one(
            span: SourceSpan, target: RationalFrameRate
        ) -> CfrConversionReport:
            report = real(span, target)
            return report.model_copy(update={"output_frames": report.output_frames + 1})

        runner_module.expected_conversion = off_by_one
        try:
            return self._normalize()
        finally:
            runner_module.expected_conversion = real

    def _hash_confusion(self) -> int:
        manifest = build_source_manifest(self.arguments)
        record = normalize_one(
            manifest,
            self.arguments.target,
            NormalizeContext(
                lock_path=self.arguments.lock,
                ffmpeg=self.arguments.ffmpeg,
                ffprobe=self.arguments.ffprobe,
                output_dir=self.arguments.output_dir,
                record_out=self.arguments.out,
            ),
        )
        confused = record.model_copy(
            update={
                "output": record.output.model_copy(
                    update={"sha256": record.source.sha256}
                )
            }
        )
        try:
            NormalizeRecord.model_validate_json(confused.model_dump_json())
        except ValidationError as error:
            print("reason=hash_confusion")
            print(f"error=record_invalid: {error.errors()[0]['type']}")
            return 2
        print("hash-confusion fault was not detected")
        return 1
