"""The r8 keep_remove_contradiction prompt constraint (string-level).

The live director proposed one span carrying both keep and remove intents
without a parent relation; the semantic validator refused it. The v1 prompt
now states that rule explicitly, and a bounded retry carries the refusal
back inside the prompt bundle as DATA.
"""

from __future__ import annotations

from services.cli.real_director import refusal_feedback_text
from services.editorial.prompt import SYSTEM_PROMPT, build_prompt
from services.foundation_io import canonical_model_bytes
from tests.editorial.support import load_manifest, manifest_request


def test_system_prompt_forbids_keep_remove_coexistence() -> None:
    assert "One span must not carry both keep and remove intents" in SYSTEM_PROMPT
    assert "parent relation" in SYSTEM_PROMPT


def test_first_attempt_bundle_carries_no_feedback() -> None:
    request = manifest_request(load_manifest("p1-ref-01-clean-ja"))

    assert request.refusal_feedback is None
    assert build_prompt(request).refusal_feedback is None


def test_retry_feedback_rides_the_bundle_as_data() -> None:
    reason = "keep_remove_contradiction: span ('src', 'ab') carries both intents"
    feedback = refusal_feedback_text(reason)
    request = manifest_request(load_manifest("p1-ref-01-clean-ja")).model_copy(
        update={"refusal_feedback": feedback}
    )

    bundle = build_prompt(request)

    assert bundle.refusal_feedback == feedback
    assert reason in bundle.refusal_feedback
    assert feedback in canonical_model_bytes(bundle).decode("utf-8")
