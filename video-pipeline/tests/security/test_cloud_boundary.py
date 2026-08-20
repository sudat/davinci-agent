"""Attack class 6: cloud data violations with local_only episodes.

A local_only episode never ships data to a cloud stage: the transport
authorizer denies, the honest audit records the denial as a local routing,
and zero cloud records exist afterwards. The live transport stub refuses
under every env combination (deny-by-default) — no network call is even
possible in this suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.audit.outbound import (
    DataClass,
    OutboundRequestAudit,
    OutboundRequestInput,
)
from services.config.models import (
    BudgetPolicy,
    CloudAllowlistEntry,
    EpisodeConfig,
    NetworkPosture,
    PathAllowlist,
    RetentionPolicy,
    StageDataClasses,
    SystemConfig,
)
from services.config.resolver import resolve
from services.policy.data_policy import authorize_cloud_transport
from services.review_command.translator_transport import (
    CREDENTIALS_ENV,
    POLICY_ENV,
    LiveTransport,
    TransportFailure,
)
from tests.security.support import assert_zero_side_effects, snapshot_tree

PRODUCTION_EPISODE = "ep-cloud-attack-42"


def _resolved(cloud_allowlist: tuple[CloudAllowlistEntry, ...]):
    system = SystemConfig(
        schema_version="system-config-v1",
        retention=RetentionPolicy(authoritative="permanent", rebuildable_days=30),
        data_classes=(
            StageDataClasses(stage="review_translate", classes=("review_instruction_text",)),
        ),
        cloud_allowlist=cloud_allowlist,
        network=NetworkPosture(builder="loopback", builder_endpoint="127.0.0.1:8432"),
        path_allowlist=PathAllowlist(roots=("/pipeline/jobs",)),
        budget=BudgetPolicy(
            transient_max_attempts=3,
            permanent_max_attempts=1,
            blocking_human_max_attempts=1,
            max_stage_cost_units=1000,
            max_job_cost_units=10000,
        ),
    )
    return resolve(system, episode=EpisodeConfig(episode_id=PRODUCTION_EPISODE))


def test_10_local_only_episode_cloud_stage_denied_zero_cloud_records(
    tmp_path: Path,
) -> None:
    resolved = _resolved(
        (CloudAllowlistEntry(
            data_class="review_instruction_text", stage="review_translate", fixture_only=True
        ),)
    )
    decision = authorize_cloud_transport(
        resolved,
        data_class="review_instruction_text",
        stage="review_translate",
        episode_id=PRODUCTION_EPISODE,
    )
    assert decision.decision == "deny"
    assert "local_only" in decision.reason

    audit_path = tmp_path / "outbound-audit.jsonl"
    audit = OutboundRequestAudit(audit_path)
    audit.record_request(
        OutboundRequestInput(
            destination_class="local_tool",
            endpoint_declared="unix:///run/local-translator.sock",
            data_classes=("review_instruction_text",),
            policy_decision=decision.decision,
            policy_reason=decision.reason,
            request_sha256="d" * 64,
            timestamp_seq=1,
        )
    )
    assert audit.audit_query(destination_class="cloud") == ()
    raw = audit_path.read_text(encoding="utf-8")
    assert '"policy_decision":"deny"' in raw.replace(" ", "")


def test_11_denied_transport_writes_no_audit_file(tmp_path: Path) -> None:
    resolved = _resolved(())
    decision = authorize_cloud_transport(
        resolved,
        data_class="transcript",
        stage="asr_upload",
        episode_id=PRODUCTION_EPISODE,
    )
    assert decision.decision == "deny"
    before = snapshot_tree(tmp_path)
    assert decision.allowed is False
    assert_zero_side_effects(tmp_path, before)
    assert not (tmp_path / "outbound-audit.jsonl").exists()


def test_20_live_transport_refuses_without_policy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(POLICY_ENV, raising=False)
    monkeypatch.delenv(CREDENTIALS_ENV, raising=False)
    outcome = LiveTransport().send("a" * 64)
    assert isinstance(outcome, TransportFailure)
    assert outcome.code == "live_disabled"


def test_21_live_transport_refuses_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(POLICY_ENV, "1")
    monkeypatch.delenv(CREDENTIALS_ENV, raising=False)
    outcome = LiveTransport().send("a" * 64)
    assert isinstance(outcome, TransportFailure)
    assert outcome.code == "no_credentials"
    before = snapshot_tree(tmp_path)
    assert_zero_side_effects(tmp_path, before)


def test_22_live_transport_still_refuses_with_both_envs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = tmp_path / "creds.json"
    creds.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(POLICY_ENV, "1")
    monkeypatch.setenv(CREDENTIALS_ENV, str(creds))
    outcome = LiveTransport().send("a" * 64)
    assert isinstance(outcome, TransportFailure)
    assert outcome.code == "model_not_pinned"


def test_30_stage_not_declared_in_episode_config_denied() -> None:
    resolved = _resolved(())
    decision = authorize_cloud_transport(
        resolved,
        data_class="ocr_text",
        stage="undeclared_cloud_stage",
        episode_id=PRODUCTION_EPISODE,
    )
    assert decision.decision == "deny"
    assert decision.allowed is False


@pytest.mark.parametrize(
    "data_class", ["transcript", "ocr_text", "audio", "original_video"]
)
def test_31_sensitive_classes_never_allowlisted_for_production(
    data_class: DataClass,
) -> None:
    resolved = _resolved(
        (CloudAllowlistEntry(
            data_class="review_instruction_text", stage="review_translate", fixture_only=True
        ),)
    )
    decision = authorize_cloud_transport(
        resolved,
        data_class=data_class,
        stage="review_translate",
        episode_id=PRODUCTION_EPISODE,
    )
    assert decision.decision == "deny"
