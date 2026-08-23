"""Production editorial model provider (task 3, v44-first-publish-delta).

Tier A against the synthetic W3 fixture episode — NO network, NO real
model: the transport is always an injected fake replaying the heuristic
baseline's own drafts (the ``clean_fake`` pattern from test_director_v2).

Proves:
(a) ``build_llm_call`` over a fake ``http_post`` completes a valid
    ThreePassResult with per-pass structured-output bodies (prompt text +
    the request serialized as DATA, ``strict: true`` json_schema per pass);
(b) malformed / non-JSON / non-object model responses are typed
    ``model-bad-response`` (never a heuristic fallback);
(c) transport failures map to the typed codes: HTTP error status →
    ``model-http-status``, refused redirect → ``model-redirect-refused``,
    timeout → ``model-timeout``;
(d) production mode with NO transport is typed
    ``production-model-unavailable``;
(e) the env gate (``make_http_post``) refuses BEFORE any socket and
    ``episode0._stage_director`` escalates to the typed BLOCKED error,
    while ``heuristic_diagnostic`` runs the deterministic planner behind
    an explicit banner;
(f) ``config/editorial-runtime.json`` + the three pins validate, resolve,
    and stay three INDEPENDENT logical pins.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from pydantic import ValidationError

from services.cli.episode0 import Episode0BlockedError, _stage_director
from services.cli.live_editorial_v2 import (
    CREDENTIALS_ENV,
    NETWORK_ENV,
    REQUEST_TIMEOUT_SECONDS,
    EditorialTransportGatedError,
    make_http_post,
)
from services.editorial_v2.director_v2 import DirectorV2, ThreePassResult
from services.editorial_v2.model_provider import (
    DIRECTOR_PIN_PATH,
    EditorialHttpResponseError,
    EditorialPinV2,
    EditorialRedirectRefusedError,
    EditorialRuntimeError,
    EditorialRuntimeV1,
    build_llm_call,
    load_editorial_pin,
    load_editorial_runtime,
)
from services.editorial_v2.prompt_v2 import PROMPT_A, StoryPlanDraft
from services.foundation_io import atomic_write, canonical_model_bytes
from tests.editorial_v2.fixtures.three_pass_fixture import (
    make_brief,
    make_episode_artifact,
    open_api,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from services.media_query.query_v2 import MediaQueryApiV2


@pytest.fixture
def api(tmp_path: Path) -> Iterator[MediaQueryApiV2]:
    with open_api(make_episode_artifact(), tmp_path) as opened:
        yield opened


def _run(api: MediaQueryApiV2) -> ThreePassResult:
    return DirectorV2().run_three_pass(make_brief(), api)


def _director_pin() -> EditorialPinV2:
    return load_editorial_pin(DIRECTOR_PIN_PATH)


def _responses_document(payload: object) -> bytes:
    """An OpenAI-Responses-shaped document carrying one structured output."""

    return json.dumps({"output_text": json.dumps(payload, separators=(",", ":"))}).encode()


def _canned_results(baseline: ThreePassResult) -> list[bytes]:
    """The heuristic baseline's own drafts, replayed per pass (clean_fake)."""

    return [
        _responses_document({"story_plan": baseline.story_plan.model_dump(mode="json")}),
        _responses_document(baseline.moment_selection.model_dump(mode="json")),
        _responses_document(baseline.creative_edit.model_dump(mode="json")),
    ]


@dataclass(slots=True)
class _RecordedCall:
    url: str
    headers: dict[str, str]
    body: bytes
    timeout_s: float


class _ScriptedHttpPost:
    """Fake transport: replays canned bodies/exceptions, records every call."""

    def __init__(self, results: Sequence[bytes | BaseException]) -> None:
        self._results = list(results)
        self.calls: list[_RecordedCall] = []

    def __call__(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout_s: float
    ) -> bytes:
        self.calls.append(_RecordedCall(url, dict(headers), body, timeout_s))
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _write_runtime(
    tmp_path: Path, mode: Literal["production_model", "heuristic_diagnostic"]
) -> Path:
    runtime = EditorialRuntimeV1(
        schema_version="editorial-runtime-v1",
        mode=mode,
        director_pin_path=str(DIRECTOR_PIN_PATH),
        moment_review_pin_path="config/toolchains/pins/moment-review-multimodal.json",
        review_interpreter_pin_path="config/toolchains/pins/review-interpreter.json",
    )
    path = tmp_path / f"editorial-runtime-{mode}.json"
    atomic_write(path, canonical_model_bytes(runtime))
    return path


# --------------------------------------------- (a) happy path over fake transport


def test_production_three_pass_completes_over_injected_transport(
    api: MediaQueryApiV2,
) -> None:
    """Given: a scripted transport replaying the baseline drafts; When:
    DirectorV2 runs with the built llm_call; Then: a valid ThreePassResult
    and three per-pass structured-output POSTs against the pinned pin."""

    baseline = _run(api)
    http = _ScriptedHttpPost(_canned_results(baseline))
    built = build_llm_call(_director_pin(), http)
    assert callable(built)

    through = DirectorV2().run_three_pass(make_brief(), api, llm_call=built)

    assert isinstance(through, ThreePassResult)
    assert through.moment_selection.proposal == baseline.moment_selection.proposal
    assert through.story_plan == baseline.story_plan
    assert len(http.calls) == 3

    pin = _director_pin()
    first = http.calls[0]
    assert first.url == pin.endpoint
    assert first.headers["Content-Type"] == "application/json"
    # The provider seam carries no credential — the bearer lives ONLY in the
    # CLI transport, added at the socket boundary.
    assert "Authorization" not in first.headers
    assert first.timeout_s == REQUEST_TIMEOUT_SECONDS == 120

    document = json.loads(first.body)
    assert document["model"] == pin.model_id
    assert document["input"][0] == {"role": "system", "content": PROMPT_A}
    user = document["input"][1]
    assert user["role"] == "user"
    # The request rides as serialized DATA (a JSON document), never merged
    # into the instruction text — prompt-injection text stays inert data.
    request_payload = json.loads(user["content"])
    assert request_payload["stage"] == "pass_a"
    assert request_payload["brief"]["episode_id"] == baseline.story_plan.episode_id
    assert document["text"]["format"]["type"] == "json_schema"
    assert document["text"]["format"]["strict"] is True
    assert document["text"]["format"]["name"] == "StoryPlanDraft"
    assert document["text"]["format"]["schema"] == StoryPlanDraft.model_json_schema()
    assert json.loads(http.calls[1].body)["text"]["format"]["name"] == "MomentSelectionDraft"
    assert json.loads(http.calls[2].body)["text"]["format"]["name"] == "CreativeEditDraft"


# --------------------------------------------- (b) malformed responses are typed


@pytest.mark.parametrize(
    "body",
    [
        b"not json at all",
        b"{}",
        b'{"output": []}',
        b'{"output_text": 42}',
        b'{"output_text": ""}',
        b'{"output_text": "[1, 2, 3]"}',
        b'{"output_text": "null"}',
        b'{"output_text": "\\"bare string\\""}',
    ],
)
def test_malformed_model_responses_are_typed_bad_response(
    api: MediaQueryApiV2, body: bytes
) -> None:
    """Given: a transport answering garbage; Then: typed model-bad-response
    on the FIRST pass — never a heuristic fallback."""

    http = _ScriptedHttpPost([body])
    built = build_llm_call(_director_pin(), http)
    with pytest.raises(EditorialRuntimeError) as error:
        DirectorV2().run_three_pass(make_brief(), api, llm_call=built)
    assert error.value.code == "model-bad-response"
    assert len(http.calls) == 1


def test_story_plan_validation_still_lives_in_director(api: MediaQueryApiV2) -> None:
    """Given: a well-formed JSON object that violates the draft contract;
    Then: the director's own validation refuses it (seam stays raw)."""

    http = _ScriptedHttpPost([_responses_document({"story_plan": {"bogus": True}})])
    built = build_llm_call(_director_pin(), http)
    with pytest.raises(ValidationError):
        DirectorV2().run_three_pass(make_brief(), api, llm_call=built)


# --------------------------------------------- (c) transport failures map to codes


def test_http_error_status_is_typed(api: MediaQueryApiV2) -> None:
    http = _ScriptedHttpPost([EditorialHttpResponseError(429, "rate limited")])
    built = build_llm_call(_director_pin(), http)
    with pytest.raises(EditorialRuntimeError) as error:
        DirectorV2().run_three_pass(make_brief(), api, llm_call=built)
    assert error.value.code == "model-http-status"
    assert "429" in error.value.detail
    assert "rate limited" in error.value.detail


def test_refused_redirect_is_typed(api: MediaQueryApiV2) -> None:
    http = _ScriptedHttpPost([EditorialRedirectRefusedError("https://evil.example/x")])
    built = build_llm_call(_director_pin(), http)
    with pytest.raises(EditorialRuntimeError) as error:
        DirectorV2().run_three_pass(make_brief(), api, llm_call=built)
    assert error.value.code == "model-redirect-refused"
    assert "evil.example" in error.value.detail


def test_timeout_is_typed(api: MediaQueryApiV2) -> None:
    http = _ScriptedHttpPost([TimeoutError("hung")])
    built = build_llm_call(_director_pin(), http)
    with pytest.raises(EditorialRuntimeError) as error:
        DirectorV2().run_three_pass(make_brief(), api, llm_call=built)
    assert error.value.code == "model-timeout"
    assert "120" in error.value.detail


# --------------------------------------------- (d,e) BLOCKED semantics + env gate


def test_missing_transport_in_production_mode_is_typed_unavailable() -> None:
    """Given: production mode with NO constructible transport; Then: typed
    production-model-unavailable — never a heuristic fallback."""

    with pytest.raises(EditorialRuntimeError) as error:
        build_llm_call(_director_pin(), None)
    assert error.value.code == "production-model-unavailable"
    assert "heuristic" in error.value.detail


def test_drifted_pin_schema_record_is_refused() -> None:
    pin = _director_pin().model_copy(
        update={"structured_output_schemas": {"pass_a": "NotADraft"}}
    )
    with pytest.raises(EditorialRuntimeError) as error:
        build_llm_call(pin, _ScriptedHttpPost([]))
    assert error.value.code == "production-model-unavailable"


def test_gate_refuses_without_credentials_before_any_socket() -> None:
    with pytest.raises(EditorialTransportGatedError) as error:
        make_http_post({})
    assert error.value.code == "no-credentials"
    assert CREDENTIALS_ENV in error.value.detail


def test_gate_refuses_without_network_flag_before_any_socket() -> None:
    with pytest.raises(EditorialTransportGatedError) as error:
        make_http_post({CREDENTIALS_ENV: "test-key"})
    assert error.value.code == "live-disabled"
    assert NETWORK_ENV in error.value.detail


def test_gate_constructs_transport_when_both_envs_are_set() -> None:
    """Given: both gate envs; Then: a callable transport is constructed —
    and (by construction) no call is made here, so no socket opens."""

    transport = make_http_post({CREDENTIALS_ENV: "test-key", NETWORK_ENV: "1"})
    assert callable(transport)


def test_stage_director_production_without_credentials_blocks_zero_socket(
    api: MediaQueryApiV2, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given: production runtime + cleared env; Then: typed BLOCKED with the
    missing env NAMES, and no socket layer is even constructed."""

    runtime = _write_runtime(tmp_path, "production_model")

    def no_socket(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a urllib opener was constructed — the gate must fire first")

    monkeypatch.setattr("urllib.request.build_opener", no_socket)
    with pytest.raises(Episode0BlockedError) as error:
        _stage_director(make_brief(), api, None, runtime_path=runtime, env={})
    assert error.value.code == "production-model-unavailable"
    assert CREDENTIALS_ENV in error.value.detail
    assert NETWORK_ENV in error.value.detail


def test_stage_director_production_mode_wires_injected_transport(
    api: MediaQueryApiV2, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given: production runtime + a monkeypatched transport factory; Then:
    _stage_director wires the pinned llm through DirectorV2 end-to-end."""

    runtime = _write_runtime(tmp_path, "production_model")
    baseline = _run(api)
    http = _ScriptedHttpPost(_canned_results(baseline))
    monkeypatch.setattr("services.cli.episode0.make_http_post", lambda env=None: http)

    result = _stage_director(
        make_brief(),
        api,
        None,
        runtime_path=runtime,
        env={CREDENTIALS_ENV: "test-key", NETWORK_ENV: "1"},
    )

    assert result.moment_selection.proposal == baseline.moment_selection.proposal
    assert len(http.calls) == 3


def test_stage_director_heuristic_mode_runs_deterministic_planner(
    api: MediaQueryApiV2, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Given: heuristic_diagnostic runtime; Then: llm_call=None planner output
    byte-identical to the default, behind an explicit diagnostic banner."""

    runtime = _write_runtime(tmp_path, "heuristic_diagnostic")
    result = _stage_director(make_brief(), api, None, runtime_path=runtime)
    assert result.model_dump_json() == _run(api).model_dump_json()
    out = capsys.readouterr().out
    assert "heuristic_diagnostic" in out
    assert "llm_call=None" in out


# --------------------------------------------- (f) shipped config validates


def test_default_runtime_and_pins_validate_and_resolve() -> None:
    runtime = load_editorial_runtime()
    assert runtime.mode == "production_model"
    paths = {
        runtime.director_pin_path,
        runtime.moment_review_pin_path,
        runtime.review_interpreter_pin_path,
    }
    assert len(paths) == 3  # three INDEPENDENT logical pins (user decision)
    for relative in paths:
        assert Path(relative).is_file(), relative

    director = load_editorial_pin(Path(runtime.director_pin_path))
    assert director.purpose == "editorial-director-v2"
    assert director.model_id == "gpt-5.6-sol"
    assert director.api_surface == "openai-responses-structured-output"
    assert director.endpoint == "https://api.openai.com/v1/responses"
    assert director.external_credentials == ("EDITORIAL_DIRECTOR_API_KEY",)
    assert director.network_env == "EDITORIAL_DIRECTOR_NETWORK_ENABLED"
    assert director.structured_output_schemas == {
        "pass_a": "StoryPlanDraft",
        "pass_b": "MomentSelectionDraft",
        "pass_c": "CreativeEditDraft",
    }

    moment = load_editorial_pin(Path(runtime.moment_review_pin_path))
    assert moment.purpose == "moment-review-multimodal"
    assert moment.structured_output_schemas is None

    interpreter = load_editorial_pin(Path(runtime.review_interpreter_pin_path))
    assert interpreter.purpose == "review-interpreter"
    assert interpreter.structured_output_schemas is None


def test_pin_rejects_credential_values_and_plain_http_endpoints() -> None:
    base = {
        "schema_version": "editorial-pin-v2",
        "purpose": "editorial-director-v2",
        "model_id": "gpt-5.6-sol",
        "api_surface": "openai-responses-structured-output",
        "endpoint": "https://api.openai.com/v1/responses",
        "external_credentials": ("EDITORIAL_DIRECTOR_API_KEY",),
        "network_env": "EDITORIAL_DIRECTOR_NETWORK_ENABLED",
    }
    with_http = dict(base, endpoint="http://api.openai.com/v1/responses")
    with pytest.raises(ValidationError):
        EditorialPinV2.model_validate(with_http)
    with_value = dict(base, external_credentials=["EDITORIAL_DIRECTOR_API_KEY=secret"])
    with pytest.raises(ValidationError):
        EditorialPinV2.model_validate(with_value)
    with_lower = dict(base, network_env="editorial_director_network_enabled")
    with pytest.raises(ValidationError):
        EditorialPinV2.model_validate(with_lower)
    EditorialPinV2.model_validate(base)  # the shipped shape stays valid


def test_runtime_rejects_absolute_and_parent_pin_paths() -> None:
    base = {
        "schema_version": "editorial-runtime-v1",
        "mode": "production_model",
        "director_pin_path": "config/toolchains/pins/editorial-director-v2.json",
        "moment_review_pin_path": "config/toolchains/pins/moment-review-multimodal.json",
        "review_interpreter_pin_path": "config/toolchains/pins/review-interpreter.json",
    }
    with_absolute = dict(base, director_pin_path="/etc/passwd")
    with pytest.raises(ValidationError):
        EditorialRuntimeV1.model_validate(with_absolute)
    with_parent = dict(base, review_interpreter_pin_path="config/../secrets.json")
    with pytest.raises(ValidationError):
        EditorialRuntimeV1.model_validate(with_parent)
    with_unknown_mode = dict(base, mode=" vibes ")
    with pytest.raises(ValidationError):
        EditorialRuntimeV1.model_validate(with_unknown_mode)


def test_runtime_with_missing_pin_file_is_typed_unavailable(tmp_path: Path) -> None:
    runtime = EditorialRuntimeV1(
        schema_version="editorial-runtime-v1",
        mode="heuristic_diagnostic",
        director_pin_path="config/toolchains/pins/does-not-exist.json",
        moment_review_pin_path="config/toolchains/pins/moment-review-multimodal.json",
        review_interpreter_pin_path="config/toolchains/pins/review-interpreter.json",
    )
    path = tmp_path / "runtime-missing-pin.json"
    atomic_write(path, canonical_model_bytes(runtime))
    with pytest.raises(EditorialRuntimeError) as error:
        load_editorial_runtime(path)
    assert error.value.code == "production-model-unavailable"
    assert "does-not-exist.json" in error.value.detail
