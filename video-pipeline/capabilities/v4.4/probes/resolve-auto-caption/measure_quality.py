# noqa: INP001 (evidence tree is not an importable package by design)
"""``--measure | --selfcheck`` CLI for the Task 2 quality measurement.

``--measure`` scores the saved private r1/r2 Resolve auto-caption readbacks
against the corrected reference with the four frozen thresholds (no Resolve,
no regeneration, no third run) and prints counts/hashes/labels only.
``--selfcheck`` runs the fabricated-row pure-logic verification.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent
VIDEO_PIPELINE = EVIDENCE.parents[3]
for _path in (VIDEO_PIPELINE, EVIDENCE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import quality_logic  # noqa: E402 (path bootstrap first — task4 pattern)
import quality_selfcheck  # noqa: E402 (path bootstrap first)
import quality_wiring  # noqa: E402 (path bootstrap first)

from services.cli._v44_jp_metrics import JpMetricsError  # noqa: E402
from services.creative_plan.subtitle_text import (  # noqa: E402
    ProperNounDictionaryError,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="measure_quality",
        description="Task 2 quality measurement (counts-only output)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--measure", action="store_true",
        help="score private r1/r2 readbacks against the corrected reference")
    mode.add_argument(
        "--selfcheck", action="store_true",
        help="fabricated-row pure-logic verification")
    args = parser.parse_args(argv)
    if args.selfcheck:
        return quality_selfcheck.run_selfcheck()
    try:
        _report, lines = quality_wiring.measure()
    except quality_logic.QualityRefusalError as error:
        print(f"quality-refused: {error.reason}", file=sys.stderr)
        return 1
    except JpMetricsError as error:
        print(f"sample-error: {error.label}", file=sys.stderr)
        return 1
    except ProperNounDictionaryError:
        print("sample-error: dictionary-unreadable", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
