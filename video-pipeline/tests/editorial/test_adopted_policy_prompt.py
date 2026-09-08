"""Consultation slice 2: the adopted policy rides the director request.

The deterministic extraction is covered at the store level; here the
contract is the prompt/request construction: without a policy the bundle
carries no policy text (pre-slice-2 bytes), with a policy the live prompt
carries the labeled constraint text. The deterministic baseline never
receives the request at all (honest failure upstream).
"""

from __future__ import annotations

from services.editorial.models import AdoptedPolicyScopeV1, AdoptedPolicySummaryV1
from services.editorial.prompt import (
    ADOPTED_POLICY_LABEL,
    build_prompt,
    render_adopted_policy_text,
)
from tests.editorial.support import load_manifest, manifest_request


def _summary() -> AdoptedPolicySummaryV1:
    return AdoptedPolicySummaryV1(
        decision="adopt",
        scope=AdoptedPolicyScopeV1(composition=True, appearance=False, audio=True),
        structure="導入→本編→締め",
        candidate_scenes=("opening",),
        subtitle_policy="短めの字幕",
        audio_policy="BGM小さめ",
        tempo_policy="前半テンポ重視",
        unused_reasons="未使用素材はなし",
        unconfirmed=("尺の希望",),
        note="構成と音だけ採用",
    )


def test_no_policy_renders_no_text() -> None:
    assert render_adopted_policy_text(None) is None


def test_bundle_without_policy_carries_no_policy_text() -> None:
    request = manifest_request(load_manifest("p1-ref-01-clean-ja"))

    assert request.adopted_policy is None
    assert build_prompt(request).adopted_policy_text is None


def test_bundle_with_policy_carries_labeled_constraint_text() -> None:
    request = manifest_request(load_manifest("p1-ref-01-clean-ja")).model_copy(
        update={"adopted_policy": _summary()}
    )

    text = build_prompt(request).adopted_policy_text

    assert text is not None
    assert ADOPTED_POLICY_LABEL in text
    assert "導入→本編→締め" in text
    assert "短めの字幕" in text
    assert "BGM小さめ" in text
    assert "前半テンポ重視" in text
    assert "尺の希望" in text
    assert "構成と音だけ採用" in text
    assert "composition" in text
    assert "audio" in text
