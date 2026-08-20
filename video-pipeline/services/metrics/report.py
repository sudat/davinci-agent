"""``python -m services.metrics.report`` — derive, validate, publish.

Exit codes: 0 valid report; 1 typed honesty/validation failure; 2
malformed event bundle or usage error. The report file is written in
every non-malformed case, with the typed failures recorded inside it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

from services.foundation_io import atomic_write, canonical_model_bytes
from services.metrics.bundle import MetricsBundleError, load_bundle
from services.metrics.derive import derive_report
from services.metrics.report_models import ReportValidation
from services.metrics.validate import validate_report

EXIT_VALID: Final = 0
EXIT_INVALID: Final = 1
EXIT_MALFORMED: Final = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="services.metrics.report")
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        bundle = load_bundle(arguments.events)
    except (MetricsBundleError, OSError) as error:
        print(f"metrics bundle error: {error}", file=sys.stderr)
        return EXIT_MALFORMED
    report = derive_report(bundle)
    failures = validate_report(report, bundle)
    final = report.model_copy(
        update={
            "validation": ReportValidation(valid=not failures, failures=failures)
        }
    )
    atomic_write(arguments.out, canonical_model_bytes(final))
    if failures:
        for failure in failures:
            print(
                f"metrics validation failure: {failure.code}: {failure.detail}",
                file=sys.stderr,
            )
        return EXIT_INVALID
    print(
        f"metrics report written: {arguments.out} "
        f"episodes={final.eligibility.total_episodes} "
        f"denominator={final.eligibility.denominator_count} "
        f"aht_samples={final.time_metrics[-1].sample_count}"
    )
    return EXIT_VALID


if __name__ == "__main__":
    raise SystemExit(main())
