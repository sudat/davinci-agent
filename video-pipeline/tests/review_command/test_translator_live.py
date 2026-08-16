"""Live Cloud-fixture transport tests (marker ``cloud_fixture``).

Env contract (documented names, never values, never secrets):

- ``PHASE0C_TRANSLATOR_CLOUD_FIXTURE=1`` — explicit synthetic-data policy grant.
  Without it every live test skips: the translator stays deny-by-default.
- ``PHASE0C_TRANSLATOR_CREDENTIALS_FILE`` — path to a credentials file. No
  credentials exist in this repository, so the literal live run reports
  skip-with-reason, which is the honest outcome.

Even with both gates open, the frozen phase-0C toolchain pin declares
``external_model: none``, so the live transport still refuses instead of
calling an unpinned provider; the concrete production model pin arrives with
the Phase-1 editorial-model freeze (Todo 32). No test in this file can ever
emit a secret.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.review_command.translator_transport import (
    CREDENTIALS_ENV,
    POLICY_ENV,
    LiveTransport,
    TransportFailure,
)

pytestmark = pytest.mark.cloud_fixture


def _policy_granted() -> bool:
    return os.environ.get(POLICY_ENV) == "1"


def _credentials_present() -> bool:
    location = os.environ.get(CREDENTIALS_ENV)
    return bool(location) and Path(location).is_file()


def test_live_transport_disabled_without_policy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(POLICY_ENV, raising=False)
    monkeypatch.delenv(CREDENTIALS_ENV, raising=False)

    outcome = LiveTransport().send("0" * 64)

    assert isinstance(outcome, TransportFailure)
    assert outcome.code == "live_disabled"
    assert POLICY_ENV in outcome.detail


def test_live_transport_reports_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(POLICY_ENV, "1")
    monkeypatch.delenv(CREDENTIALS_ENV, raising=False)

    outcome = LiveTransport().send("0" * 64)

    assert isinstance(outcome, TransportFailure)
    assert outcome.code == "no_credentials"
    assert CREDENTIALS_ENV in outcome.detail


def test_live_call_skips_without_policy_env() -> None:
    if not _policy_granted():
        pytest.skip(
            f"live translator call disabled: synthetic-data policy env {POLICY_ENV}=1 is not set"
        )
    outcome = LiveTransport().send("0" * 64)
    assert isinstance(outcome, TransportFailure)


def test_live_call_skips_without_credentials() -> None:
    if not _policy_granted():
        pytest.skip(f"live translator call disabled: {POLICY_ENV}=1 is not set")
    if not _credentials_present():
        pytest.skip(
            f"live translator call disabled: {CREDENTIALS_ENV} does not point to a credentials "
            "file (none exists in this repository)"
        )
    outcome = LiveTransport().send("0" * 64)
    assert isinstance(outcome, TransportFailure)
