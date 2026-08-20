from __future__ import annotations

from pathlib import Path

import pytest

from services.metrics.bundle import MetricsBundleError, load_bundle
from services.metrics.derive import derive_report
from tests.metrics.support import BundleBuilder


def test_denominator_counts_real_in_contract_only(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-real-1")
    builder.episode("ep-real-2")
    builder.episode("ep-tech-1", kind="technical")
    builder.episode("ep-out-1", in_contract=False, exclusion_reason="language-out-of-contract")
    report = derive_report(load_bundle(builder.write()))

    assert report.eligibility.total_episodes == 4
    assert report.eligibility.real_episodes == 3
    assert report.eligibility.denominator_count == 2
    assert report.eligibility.coverage_ratio_percent == 66


def test_out_of_contract_episode_listed_and_hidden_from_denominator(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-real-1")
    builder.episode("ep-out-1", in_contract=False, exclusion_reason="duration-over-budget")
    report = derive_report(load_bundle(builder.write()))

    assert report.eligibility.denominator_count == 1
    exclusion = report.eligibility.exclusions[0]
    assert exclusion.episode_id == "ep-out-1"
    assert exclusion.reason == "duration-over-budget"
    assert exclusion.episode_kind == "real"


def test_exclusion_without_reason_is_malformed(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-out-1", in_contract=False, exclusion_reason=None)
    with pytest.raises(MetricsBundleError):
        load_bundle(builder.write())


def test_undeclared_episode_in_events_is_hidden(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-real-1")
    builder.timing("ep-ghost", "review", "start", 1700000000)
    report = derive_report(load_bundle(builder.write()))

    assert report.eligibility.hidden_episode_ids == ("ep-ghost",)


def test_coverage_ratio_none_without_real_episodes(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-tech-1", kind="technical")
    report = derive_report(load_bundle(builder.write()))

    assert report.eligibility.real_episodes == 0
    assert report.eligibility.coverage_ratio_percent is None


def test_technical_and_synthetic_labeling(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-real-1")
    builder.episode("ep-tech-1", kind="technical")
    builder.episode("ep-syn-1", kind="synthetic")
    report = derive_report(load_bundle(builder.write()))

    assert report.eligibility.technical_episode_ids == ("ep-tech-1",)
    assert report.eligibility.synthetic_episode_ids == ("ep-syn-1",)
    assert report.eligibility.denominator_count == 1
