# allow: SIZE_OK — 290 pure LOC: the v44 plan names ONE focused pin/runtime
# test module; every neighboring test module in this repo runs longer
# (test_model_provider.py 721, test_real_episode.py 595) and splitting the
# cohesive T3 suite (pins + grammar + gate + episode declaration) would
# fragment the acceptance evidence across files.
"""T3 — additive pin surfaces + runtime specialist path (v44 video-understanding).

Tier A against the SHIPPED config bytes — NO network, NO provider calls:

- the two live-probed network surfaces (Gemini Developer API generateContent,
  Z.AI Coding-Plan chat completions) are valid ``editorial-pin-v2`` surfaces
  that REQUIRE endpoint + credential env-name + network env-name;
- env-name fields accept ONLY conventional UPPER_SNAKE names with at least
  one underscore — secret-shaped all-uppercase values are refused;
- ``moment-review-multimodal`` is pinned to ``gemini-3.7-flash`` (the
  lead/reduce/fusion pin); a NEW independent ``moment-review-glm-visual``
  pin carries the audio-stripped specialist;
- ``EditorialRuntimeV1`` gains optional ``moment_review_specialist_pin_path``
  so old runtime payloads stay parseable; the shipped runtime names it;
- ``require_pin_env`` gates one network pin against an injected env mapping:
  deny-by-default, typed ``production-model-unavailable`` naming env-var
  NAMES ONLY (T4 transports reuse it before any provider call);
- the representative episode's cloud-data declaration (the resolved-policy
  convention in ``services.cli.real_policy``) permits video+audio+transcript
  to the Gemini stage and original_video ONLY to the specialist stage.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.cli.real_policy import (
    MOMENT_REVIEW_SPECIALIST_STAGE,
    MOMENT_REVIEW_STAGE,
    local_only_policy,
    video_understanding_policy,
)
from services.editorial_v2.editorial_pins import (
    MOMENT_REVIEW_PIN_PATH,
    MOMENT_REVIEW_SPECIALIST_PIN_PATH,
    EditorialPinV2,
    EditorialRuntimeError,
    EditorialRuntimeV1,
    load_editorial_pin,
    load_editorial_runtime,
    require_pin_env,
)
from services.foundation_io import atomic_write, canonical_model_bytes
from services.policy.data_policy import authorize_cloud_transport

REPRESENTATIVE_EPISODE = "v44-real-01"

_GEMINI_PIN_FIELDS: dict[str, object] = {
    "schema_version": "editorial-pin-v2",
    "purpose": "moment-review-multimodal",
    "model_id": "gemini-3.7-flash",
    "api_surface": "google-gemini-developer-api-generate-content-rest-v1beta",
    "endpoint": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.7-flash:generateContent",
    "external_credentials": ("GEMINI_API_KEY",),
    "network_env": "GEMINI_NETWORK_ENABLED",
    "structured_output_schemas": None,
}

_GLM_PIN_FIELDS: dict[str, object] = {
    "schema_version": "editorial-pin-v2",
    "purpose": "moment-review-glm-visual",
    "model_id": "glm-5v-turbo",
    "api_surface": "openai-compatible-chat-completions-v4-coding-plan",
    "endpoint": "https://api.z.ai/api/coding/paas/v4/chat/completions",
    "external_credentials": ("ZAI_API_KEY",),
    "network_env": "ZAI_NETWORK_ENABLED",
    "structured_output_schemas": None,
}

_OLD_RUNTIME_PAYLOAD: dict[str, object] = {
    "schema_version": "editorial-runtime-v1",
    "mode": "production_model",
    "transport": "codex-exec",
    "director_pin_path": "config/toolchains/pins/editorial-director-v2.json",
    "moment_review_pin_path": "config/toolchains/pins/moment-review-multimodal.json",
    "review_interpreter_pin_path": "config/toolchains/pins/review-interpreter.json",
}

_CODEX_PIN_FIELDS: dict[str, object] = {
    "schema_version": "editorial-pin-v2",
    "purpose": "editorial-director-v2",
    "model_id": "gpt-5.6-sol",
    "api_surface": "codex-exec",
}


# ------------------------------- runtime compatibility (old payloads parse)


def test_old_runtime_payload_without_specialist_path_stays_parseable(
    tmp_path: Path,
) -> None:
    """Given: a pre-T3 runtime payload (no specialist key); Then: it parses
    with an explicit compatibility default (None) and round-trips loadably."""

    legacy = EditorialRuntimeV1.model_validate(_OLD_RUNTIME_PAYLOAD)
    assert legacy.moment_review_specialist_pin_path is None

    path = tmp_path / "legacy-runtime.json"
    atomic_write(path, canonical_model_bytes(legacy))
    loaded = load_editorial_runtime(path)
    assert loaded.moment_review_specialist_pin_path is None


# ------------------------------- shipped config validates (exact probed ids)


def test_shipped_runtime_and_pins_carry_exact_probed_identifiers() -> None:
    """Given: the shipped runtime + pins; Then: four INDEPENDENT pin paths,
    the Gemini lead pin and the GLM specialist pin carry the EXACT
    live-probed surface/model/endpoint/env names from the v4.4 probes."""

    runtime = load_editorial_runtime()
    paths = {
        runtime.director_pin_path,
        runtime.moment_review_pin_path,
        runtime.moment_review_specialist_pin_path,
        runtime.review_interpreter_pin_path,
    }
    assert None not in paths
    assert len(paths) == 4  # four INDEPENDENT logical pins now
    assert runtime.moment_review_specialist_pin_path == str(MOMENT_REVIEW_SPECIALIST_PIN_PATH)
    for relative in paths:
        assert Path(str(relative)).is_file(), relative

    gemini = load_editorial_pin(Path(runtime.moment_review_pin_path))
    assert gemini.model_dump() == _GEMINI_PIN_FIELDS

    specialist = load_editorial_pin(Path(str(runtime.moment_review_specialist_pin_path)))
    assert specialist.model_dump() == _GLM_PIN_FIELDS


def test_existing_openai_and_codex_surfaces_stay_valid() -> None:
    """Given: the pre-T3 surface widths; Then: the openai-responses pin shape
    still validates and codex-exec still refuses endpoint/credential fields."""

    openai_shape = EditorialPinV2.model_validate(
        {
            "schema_version": "editorial-pin-v2",
            "purpose": "editorial-director-v2",
            "model_id": "gpt-5.6-sol",
            "api_surface": "openai-responses-structured-output",
            "endpoint": "https://api.openai.com/v1/responses",
            "external_credentials": ("EDITORIAL_DIRECTOR_API_KEY",),
            "network_env": "EDITORIAL_DIRECTOR_NETWORK_ENABLED",
        }
    )
    assert openai_shape.model_id == "gpt-5.6-sol"
    with pytest.raises(ValidationError):
        EditorialPinV2.model_validate(
            dict(_CODEX_PIN_FIELDS, endpoint="https://api.openai.com/v1/responses")
        )


# ------------------------------- env-name grammar (names only, never values)


@pytest.mark.parametrize(
    "bad_name",
    [
        "ABC1234567890",  # all-uppercase secret-shaped value without underscore
        "GEMINI API KEY",
        "GEMINI_API_KEY=secret",
        "gemini_api_key",
        "1GEMINI_API_KEY",
        "GEMINI__API_KEY",
        "GEMINI_API_KEY_",
    ],
)
def test_env_name_grammar_rejects_secret_shaped_and_malformed_entries(
    bad_name: str,
) -> None:
    """Given: a credential entry that is not a conventional env-var NAME
    (secret-shaped, spaced, key=value, lowercase, malformed underscores);
    Then: refused on every network surface — names only, never values."""

    with pytest.raises(ValidationError):
        EditorialPinV2.model_validate(dict(_GEMINI_PIN_FIELDS, external_credentials=(bad_name,)))
    with pytest.raises(ValidationError):
        EditorialPinV2.model_validate(dict(_GLM_PIN_FIELDS, network_env=bad_name))


@pytest.mark.parametrize(
    "shipped_name",
    ["GEMINI_API_KEY", "ZAI_API_KEY", "GEMINI_NETWORK_ENABLED", "ZAI_NETWORK_ENABLED"],
)
def test_env_name_grammar_accepts_the_shipped_gate_names(shipped_name: str) -> None:
    """Given: the four shipped Gemini/GLM env names; Then: all validate."""

    EditorialPinV2.model_validate(dict(_GEMINI_PIN_FIELDS, network_env=shipped_name))


# ------------------------------- new-surface validation branches


@pytest.mark.parametrize("pin_fields", [_GEMINI_PIN_FIELDS, _GLM_PIN_FIELDS])
def test_new_surface_pins_reject_plain_http_endpoints(pin_fields: dict[str, object]) -> None:
    """Given: a new-surface pin with an http:// endpoint; Then: refused."""

    with_http = dict(pin_fields, endpoint="http://generativelanguage.googleapis.com/x")
    with pytest.raises(ValidationError):
        EditorialPinV2.model_validate(with_http)


def test_unknown_api_surface_is_rejected() -> None:
    """Given: a drifted surface name (e.g. Vertex AI); Then: refused — the
    plan forbids silent provider switching."""

    with pytest.raises(ValidationError):
        EditorialPinV2.model_validate(
            dict(_GEMINI_PIN_FIELDS, api_surface="vertex-ai-generate-content")
        )


@pytest.mark.parametrize(
    ("drop", "surface_fields"),
    [
        ("network_env", _GEMINI_PIN_FIELDS),
        ("endpoint", _GLM_PIN_FIELDS),
        ("external_credentials", _GLM_PIN_FIELDS),
    ],
)
def test_half_configured_new_surface_pin_is_refused(
    drop: str, surface_fields: dict[str, object]
) -> None:
    """Given: a new-surface pin missing one network field; Then: refused —
    a drifted half-configured pin never loads."""

    drained = {key: value for key, value in surface_fields.items() if key != drop}
    with pytest.raises(ValidationError):
        EditorialPinV2.model_validate(drained)


# ------------------------------- pin-level env gate (deny-by-default seam)


def test_disabled_network_gate_is_typed_unavailable_with_names_only() -> None:
    """Given: Gemini/GLM pins with a missing credential, a missing gate, or
    a gate not set to 1; Then: typed production-model-unavailable naming
    env-var NAMES ONLY — never a fallback, never a credential value."""

    gemini = load_editorial_pin(MOMENT_REVIEW_PIN_PATH)
    glm = load_editorial_pin(MOMENT_REVIEW_SPECIALIST_PIN_PATH)
    for pin, gate, cred in (
        (gemini, "GEMINI_NETWORK_ENABLED", "GEMINI_API_KEY"),
        (glm, "ZAI_NETWORK_ENABLED", "ZAI_API_KEY"),
    ):
        with pytest.raises(EditorialRuntimeError) as error:
            require_pin_env(pin, {})
        assert error.value.code == "production-model-unavailable"
        assert gate in error.value.detail
        assert cred in error.value.detail
    with pytest.raises(EditorialRuntimeError) as error:
        require_pin_env(glm, {"ZAI_NETWORK_ENABLED": "1"})
    assert "ZAI_API_KEY" in error.value.detail
    with pytest.raises(EditorialRuntimeError) as error:
        require_pin_env(glm, {"ZAI_API_KEY": "value-never-leaked", "ZAI_NETWORK_ENABLED": "0"})
    assert error.value.code == "production-model-unavailable"
    assert "ZAI_NETWORK_ENABLED" in error.value.detail
    assert "value-never-leaked" not in error.value.detail


def test_enabled_env_passes_gate_and_codex_pin_stays_ungated() -> None:
    """Given: a fully enabled injected env; Then: both network pins pass
    (no socket by construction) and codex-exec stays env-ungated."""

    require_pin_env(
        load_editorial_pin(MOMENT_REVIEW_PIN_PATH),
        {"GEMINI_API_KEY": "k", "GEMINI_NETWORK_ENABLED": "1"},
    )
    require_pin_env(
        load_editorial_pin(MOMENT_REVIEW_SPECIALIST_PIN_PATH),
        {"ZAI_API_KEY": "k", "ZAI_NETWORK_ENABLED": "1"},
    )
    require_pin_env(EditorialPinV2.model_validate(_CODEX_PIN_FIELDS), {})


# ------------------------------- runtime specialist-path discipline


def test_runtime_with_missing_specialist_pin_file_is_typed_unavailable(
    tmp_path: Path,
) -> None:
    """Given: a runtime naming a missing specialist pin file; Then: typed
    production-model-unavailable naming the file — never a silent default."""

    runtime = EditorialRuntimeV1.model_validate(
        dict(
            _OLD_RUNTIME_PAYLOAD,
            moment_review_specialist_pin_path="config/toolchains/pins/no-glm-pin.json",
        )
    )
    path = tmp_path / "runtime-missing-specialist.json"
    atomic_write(path, canonical_model_bytes(runtime))
    with pytest.raises(EditorialRuntimeError) as error:
        load_editorial_runtime(path)
    assert error.value.code == "production-model-unavailable"
    assert "no-glm-pin.json" in error.value.detail


def test_runtime_specialist_path_must_be_relative() -> None:
    """Given: absolute or parent-escaping specialist paths; Then: refused."""

    with pytest.raises(ValidationError):
        EditorialRuntimeV1.model_validate(
            dict(_OLD_RUNTIME_PAYLOAD, moment_review_specialist_pin_path="/etc/passwd")
        )
    with pytest.raises(ValidationError):
        EditorialRuntimeV1.model_validate(
            dict(
                _OLD_RUNTIME_PAYLOAD,
                moment_review_specialist_pin_path="config/../secrets.json",
            )
        )


def test_noncanonical_pin_bytes_are_typed_unavailable(tmp_path: Path) -> None:
    """Given: semantically valid pin bytes that are NOT canonical; Then: the
    canonical loader refuses them typed (loader seam, unchanged)."""

    pretty = json.dumps(_GLM_PIN_FIELDS, sort_keys=True, indent=2).encode()
    path = tmp_path / "pretty-pin.json"
    atomic_write(path, pretty)
    with pytest.raises(EditorialRuntimeError) as error:
        load_editorial_pin(path)
    assert error.value.code == "production-model-unavailable"
    assert "not canonical JSON" in error.value.detail


# ------------------------------- episode cloud-data declaration (T3 scope)


def test_video_understanding_policy_grants_gemini_av_and_glm_video_only() -> None:
    """Given: the representative episode's video-understanding declaration;
    Then: video+audio+transcript may leave for the Gemini moment-review
    stage, original_video ONLY for the specialist stage, and audio for the
    specialist stage is DENIED by the existing typed seam."""

    policy = video_understanding_policy(REPRESENTATIVE_EPISODE)
    assert policy.episode_id == REPRESENTATIVE_EPISODE
    granted = {(entry.data_class, entry.stage) for entry in policy.cloud_allowlist}
    assert ("original_video", MOMENT_REVIEW_STAGE) in granted
    assert ("audio", MOMENT_REVIEW_STAGE) in granted
    assert ("transcript", MOMENT_REVIEW_STAGE) in granted
    assert ("original_video", MOMENT_REVIEW_SPECIALIST_STAGE) in granted
    assert ("audio", MOMENT_REVIEW_SPECIALIST_STAGE) not in granted

    allowed = authorize_cloud_transport(
        policy,
        data_class="original_video",
        stage=MOMENT_REVIEW_SPECIALIST_STAGE,
        episode_id=REPRESENTATIVE_EPISODE,
    )
    assert allowed.allowed
    audio_to_specialist = authorize_cloud_transport(
        policy,
        data_class="audio",
        stage=MOMENT_REVIEW_SPECIALIST_STAGE,
        episode_id=REPRESENTATIVE_EPISODE,
    )
    assert not audio_to_specialist.allowed


def test_local_only_default_stays_cloud_free() -> None:
    """Regression: the shipped default posture grants NO cloud classes."""

    assert local_only_policy(REPRESENTATIVE_EPISODE).cloud_allowlist == ()
