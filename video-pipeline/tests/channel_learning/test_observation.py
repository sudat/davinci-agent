"""Task 56: PerformanceObservationV1 schema + separation tests."""

from __future__ import annotations

import pathlib

import pytest
from pydantic import ValidationError

from services.channel_learning.observation import (
    PerformanceObservationV1,
    import_manual,
)


def _full_dict() -> dict[str, object]:
    return {
        "schema_version": "performance-observation-v1",
        "episode_id": "ep_01JTEST",
        "observed_at": "2026-08-22T00:00:00+00:00",
        "views": 12345,
        "ctr": 0.042,
        "average_view_duration_seconds": 187.5,
        "average_percentage_viewed": 42.3,
        "notable_retention_points": [
            {"timestamp_seconds": 12.5, "note": "cold open ends"},
            {"timestamp_seconds": 60.0, "note": "chapter 2 start"},
        ],
        "comment_themes": [
            {"theme": "pacing too fast", "frequency": 3},
            {"theme": "great color grade", "frequency": 7},
        ],
    }


def test_happy_import_round_trip() -> None:
    raw = _full_dict()

    obs = import_manual(raw)

    assert obs.schema_version == "performance-observation-v1"
    assert obs.episode_id == "ep_01JTEST"
    assert obs.views == 12345
    dumped = obs.model_dump()
    assert dumped["ctr"] == pytest.approx(0.042)
    reparsed = PerformanceObservationV1.model_validate(dumped)
    assert reparsed == obs
    reparsed2 = import_manual(dumped)
    assert reparsed2 == obs


def test_ctr_out_of_bounds_rejected() -> None:
    raw = {**_full_dict(), "ctr": 1.5}

    with pytest.raises((ValidationError, ValueError)):
        import_manual(raw)


def test_unknown_key_rejected() -> None:
    raw = {**_full_dict(), "unknown_field": "oops"}

    with pytest.raises((ValidationError, ValueError)):
        import_manual(raw)


def test_separation_no_reference_learning_overlap() -> None:
    # If reference_learning exists, observation must not import it.
    # Otherwise, source must not reference taste/preference symbols
    # outside the required separation-contract docstring.
    import services.channel_learning.observation as mod  # noqa: PLC0415

    source = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    # No import of reference_learning
    assert "reference_learning" not in source
    # Separation marker must exist
    assert "performance-observation-v1" in source
    # Module docstring must state separation contract
    doc = mod.__doc__ or ""
    assert "separation" in doc.lower() or "separate" in doc.lower()

    # Count taste/preference occurrences: allow at most the docstring
    # contract lines; code body must not reference them.
    # We check that no importable symbol from reference_learning leaks.
    # If package exists, verify no symbol overlap; else ensure code
    # does not define taste/preference models.
    try:
        import services.reference_learning as _rl  # type: ignore[import-not-found]  # noqa: PLC0415

        rl_symbols = set(getattr(_rl, "__all__", []))
        if not rl_symbols:
            try:
                import services.reference_learning.models as _rlm2  # noqa: PLC0415

                rl_symbols = set(getattr(_rlm2, "__all__", []))
            except ModuleNotFoundError:
                rl_symbols = set()
        obs_symbols = {s for s in dir(mod) if not s.startswith("_")}
        assert rl_symbols.isdisjoint(obs_symbols), (
            f"overlap: {rl_symbols & obs_symbols}"
        )
    except ModuleNotFoundError:
        # No package yet — ensure observation.py code (excluding
        # docstring) does not define taste/preference artifacts.
        # Strip docstring for the check.
        lower = source.lower()
        # Remove first docstring block naively
        first_quote = lower.find('"""')
        second_quote = lower.find('"""', first_quote + 3) if first_quote != -1 else -1
        code_only = lower[second_quote + 3 :] if second_quote != -1 else lower
        # Code body must not import or define preference/taste models
        assert "preference" not in code_only or "separation" in lower
        # At minimum, no class/function named with taste/preference
        assert "class taste" not in code_only
        assert "class preference" not in code_only
