"""Bound 全編へ authorization (P2-1 split: the authorization concept).

Model-join/verification for the explicit full-episode authorization:
binding construction, the strict adopted-policy join, explicit sample
lookup, and guard verification. Journal primitives (append/reload/lock)
and the general policy views stay in ``consultation_store.py``, which
re-exports this module's public entry points so existing callers keep
working. No new generic framework: one concept module, same records.

Wire shape (P1-1): the full_authorized request carries the ``sample_id``
currently displayed. The server verifies THAT sample's consultation /
base / policy pins and its video bytes, then binds to it. Unspecified,
nonexistent, or other-consultation samples are typed 422 — the server
never silently binds a different (newer) sample.

Bytes rule (P1-2): sha256_file(preview.mp4) == manifest.content_sha256
is mandatory at creation AND at every guard check, deployed through
the same ``verify_recovery`` boundary the sample journal uses
(manifest / identity / sample-IR / journal bindings).

No-archaeology rule (P1-3): after excluding ONLY the trailing
full_authorized run, the IMMEDIATELY preceding judgment must be
adopt/revise — anything else (reject/both_wrong/delegate/empty scope)
means no policy (typed 422 at creation, guard False). A DELAYED
idempotent resend of an EXISTING authorization returns the existing
row WITHOUT crossing a trailing reject (still False while one
follows it): dedupe runs before policy validation.
"""

# allow: SIZE_OK — one authorization concept per file (the bound-target
# model, the strict policy join, the explicit sample lookup, and the
# creation + guard verification sharing that SAME boundary); splitting
# creation from guard would fork the unified verification P1-2 requires,
# so the ceiling is declared instead.


from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from services.episode_cockpit.errors import (
    CockpitConflictError,
    CockpitUnprocessableError,
)
from services.episode_cockpit.sample_identity import (
    SAMPLE_MANIFEST_NAME,
    SAMPLE_PREVIEW_NAME,
    SampleManifestV1,
    sample_dir,
)
from services.foundation_io import sha256_file

if TYPE_CHECKING:
    from services.contracts.primitives import Identifier
    from services.episode_cockpit.consultation_store import (
        AdoptedPolicyV1,
        ConsultationJudgmentV1,
        ConsultationScope,
    )


class FullAuthorizationBinding(NamedTuple):
    """The server-fixed target of one 全編へ authorization.

    Fixed at append time from the current review-store head
    (``base_version`` + ``base_plan_sha256``), the adopted policy
    (``policy_sha256``), and the EXPLICITLY NAMED sample's stored
    manifest (``sample_id`` + ``sample_content_sha256``) — never
    client-claimed, never latest-guessed.
    """

    sample_id: Identifier
    sample_content_sha256: str
    base_version: str
    base_plan_sha256: str
    policy_sha256: str


def _current_review_base(episode_dir: Path) -> tuple[str, str] | None:
    """The default-output head as (``vN``, plan sha); None when unreadable."""

    from services.outputs.geometry import (  # noqa: PLC0415 (deferred: review geometry at use site)
        DEFAULT_OUTPUT_ID,
        review_store_relatives,
    )
    from services.review_command.store import (  # noqa: PLC0415 (deferred: head read at use site)
        ReviewCommitError,
        load_head,
    )

    try:
        log_rel, store_rel = review_store_relatives(DEFAULT_OUTPUT_ID)
        head = load_head(
            episode_dir.joinpath(*log_rel), episode_dir.joinpath(*store_rel)
        )
    except (ReviewCommitError, OSError):
        return None
    entry = head.index.versions.get(str(head.version))
    if entry is None:
        return None
    return f"v{head.version}", entry.plan_sha256


def _strict_policy_before_authorization(
    episode_dir: Path, judgments: Sequence[ConsultationJudgmentV1]
) -> AdoptedPolicyV1 | None:
    """The adopted policy the authorization row(s) stand on (no archaeology).

    Trailing ``full_authorized`` rows authorize — they adopt nothing —
    so ONLY they are excluded; the IMMEDIATELY preceding judgment must
    then join to a policy (adopt|revise, non-empty scope, proposal on
    the table). A withdrawal (reject/both_wrong/delegate/empty scope)
    directly before yields no policy: adopt→reject→new-operation NEVER
    resurrects the old adopt across the reject.
    """

    from services.episode_cockpit.consultation_store import (  # noqa: PLC0415 (deferred: store owns the policy join)
        _join_policy,
    )

    prefix = list(judgments)
    while prefix and prefix[-1].decision == "full_authorized":
        prefix.pop()
    if not prefix:
        return None
    if prefix[-1].decision not in ("adopt", "revise"):
        return None
    return _join_policy(episode_dir, prefix[-1])


def _authorized_sample(  # noqa: PLR0913 (explicit sample-pin fields; kwargs are the pin contract)
    episode_dir: Path,
    *,
    consultation_id: str,
    sample_id: Identifier,
    base_version: str,
    base_plan_sha256: str,
    policy_sha256: str,
) -> SampleManifestV1:
    """Load and verify the EXPLICITLY NAMED sample (typed 422 on any gap).

    The manifest must parse, belong to this consultation, pin THIS base
    version + plan sha + policy sha, carry a content sha with the preview
    file present, and match its own video bytes; then the shared
    ``verify_recovery`` boundary (identity / sample-IR / journal
    bindings) covers it exactly like the sample journal does.
    """

    from services.episode_cockpit.sample_complete import (  # noqa: PLC0415 (deferred: completion owns verification)
        verify_recovery,
    )

    try:
        manifest = SampleManifestV1.model_validate_json(
            (sample_dir(episode_dir, sample_id) / SAMPLE_MANIFEST_NAME).read_bytes()
        )
    except (OSError, ValueError):
        raise CockpitUnprocessableError(
            "consultation-authorization-no-sample",
            "指定された試し動画が見つからないため、全編へを確定していません。",
        ) from None
    identity = manifest.identity
    if (
        manifest.sample_id != sample_id
        or identity.consultation_id != consultation_id
        or identity.base_version != base_version
        or identity.base_plan_sha256 != base_plan_sha256
        or identity.policy_sha256 != policy_sha256
        or manifest.content_sha256 is None
    ):
        raise CockpitUnprocessableError(
            "consultation-authorization-no-sample",
            "指定された試し動画が今の版・方針の確認ではないため、"
            "全編へを確定していません。",
        )
    preview = sample_dir(episode_dir, sample_id) / SAMPLE_PREVIEW_NAME
    if not preview.is_file():
        raise CockpitUnprocessableError(
            "consultation-authorization-no-sample",
            "指定された試し動画の映像が無いため、全編へを確定していません。",
        )
    try:
        video_sha = sha256_file(preview)
    except OSError as error:
        raise CockpitUnprocessableError(
            "consultation-authorization-no-sample",
            "指定された試し動画の映像が読めないため、全編へを確定していません。",
        ) from error
    if video_sha != manifest.content_sha256:
        raise CockpitUnprocessableError(
            "consultation-authorization-no-sample",
            "指定された試し動画の映像が記録と一致しないため、"
            "全編へを確定していません。",
        )
    verify_recovery(episode_dir, sample_id, identity)
    return manifest


def full_authorization_content_key(
    *,
    consultation_id: str,
    sample_id: str,
    scope: ConsultationScope,
    note: str | None,
) -> str:
    """The stable key for one full-authorization request (target included).

    The named sample is part of the key: the same operation against a
    DIFFERENT binding (moved head, changed policy, missing/tampered
    sample) is a conflict (typed 409) rather than a silent re-authorize,
    while the key itself stays stable across the check.
    """

    canon = json.dumps(
        {
            "consultation_id": consultation_id,
            "sample_id": sample_id,
            "decision": "full_authorized",
            "scope": {
                "composition": scope.composition,
                "appearance": scope.appearance,
                "audio": scope.audio,
            },
            "note": note,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode()).hexdigest()


def _full_authorization_binding_of(
    judgment: ConsultationJudgmentV1,
) -> FullAuthorizationBinding | None:
    """The bound target, or None for rows that bind nothing (legacy)."""

    if (
        judgment.auth_sample_id is None
        or judgment.auth_sample_content_sha256 is None
        or judgment.auth_base_version is None
        or judgment.auth_base_plan_sha256 is None
        or judgment.auth_policy_sha256 is None
    ):
        return None
    return FullAuthorizationBinding(
        sample_id=judgment.auth_sample_id,
        sample_content_sha256=judgment.auth_sample_content_sha256,
        base_version=judgment.auth_base_version,
        base_plan_sha256=judgment.auth_base_plan_sha256,
        policy_sha256=judgment.auth_policy_sha256,
    )


def _bound_sample_intact(episode_dir: Path, binding: FullAuthorizationBinding) -> bool:
    """True when the bound sample still verifies (bytes + full boundary).

    The SAME ``verify_recovery`` boundary as creation: manifest pins,
    video bytes vs content sha, identity / sample-IR re-derivation, and
    journal cross-refs. Any gap fails closed (False, never raises).
    """

    try:
        manifest = SampleManifestV1.model_validate_json(
            (
                sample_dir(episode_dir, binding.sample_id) / SAMPLE_MANIFEST_NAME
            ).read_bytes()
        )
    except (OSError, ValueError):
        return False
    if (
        manifest.sample_id != binding.sample_id
        or manifest.content_sha256 != binding.sample_content_sha256
    ):
        return False
    try:
        preview = sample_dir(episode_dir, binding.sample_id) / SAMPLE_PREVIEW_NAME
        if sha256_file(preview) != binding.sample_content_sha256:
            return False
    except OSError:
        return False
    try:
        from services.episode_cockpit.sample_complete import (  # noqa: PLC0415 (deferred: completion owns verification)
            verify_recovery,
        )

        verify_recovery(episode_dir, binding.sample_id, manifest.identity)
    except Exception:  # noqa: BLE001 (fail-closed authorization)
        return False
    return True


def _raise_target_moved() -> CockpitConflictError:
    """The same-operation/moved-target conflict (typed 409, never silent)."""

    return CockpitConflictError(
        "consultation-judgment-conflict",
        "同じ操作で異なる対象への全編へが要求されました。"
        "内容を確かめて操作を作り直してください。",
    )


def append_full_authorization_once(  # noqa: PLR0913 (explicit authorization-target fields; kwargs are the target contract)
    episode_dir: Path,
    *,
    consultation_id: str,
    sample_id: str | None,
    scope: ConsultationScope,
    note: str | None,
    operation_id: str | None = None,
) -> tuple[ConsultationJudgmentV1, bool]:
    """Append one bound 全編へ authorization unless already landed.

    Validation order: base → explicit sample_id presence → dedupe scan
    (an EXISTING same-operation row returns WITHOUT crossing a trailing
    reject; a moved target under it is a typed 409) → strict policy
    (no archaeology past withdrawals) → the NAMED sample's pins + video
    bytes + recovery boundary. Every refusal raises BEFORE any append,
    so the journal is unchanged. No client-claimed hashes anywhere: the
    binding is fixed inside the single-Writer lock from server state.
    """

    from services.episode_cockpit.consultation_store import (  # noqa: PLC0415 (deferred: store owns the journal primitives)
        append_judgment,
        canonical_policy_sha256,
        consultation_write_locked,
        load_judgments,
    )

    with consultation_write_locked(episode_dir):
        judgments = load_judgments(episode_dir)
        base = _current_review_base(episode_dir)
        if base is None:
            raise CockpitUnprocessableError(
                "consultation-authorization-no-base",
                "編集の版が読めないため、全編へを確定していません。",
            )
        base_version, base_plan_sha256 = base
        if not sample_id:
            raise CockpitUnprocessableError(
                "consultation-authorization-no-sample",
                "全編へには今見ている試し動画の指定が必要です。"
                "試し動画を確認してから送ってください。",
            )
        key = operation_id or full_authorization_content_key(
            consultation_id=consultation_id,
            sample_id=sample_id,
            scope=scope,
            note=note,
        )
        for existing in judgments:
            if existing.decision != "full_authorized":
                continue
            existing_key = existing.operation_id or full_authorization_content_key(
                consultation_id=existing.consultation_id,
                sample_id=existing.auth_sample_id or "",
                scope=existing.scope,
                note=existing.note,
            )
            if existing_key != key:
                continue
            bound = _full_authorization_binding_of(existing)
            if bound is None or (bound.base_version, bound.base_plan_sha256) != (
                base_version,
                base_plan_sha256,
            ):
                raise _raise_target_moved()
            strict = _strict_policy_before_authorization(episode_dir, judgments)
            if (
                strict is not None
                and canonical_policy_sha256(strict) != bound.policy_sha256
            ):
                raise _raise_target_moved()
            if not _bound_sample_intact(episode_dir, bound):
                raise _raise_target_moved()
            return existing, False
        policy = _strict_policy_before_authorization(episode_dir, judgments)
        if policy is None:
            raise CockpitUnprocessableError(
                "consultation-authorization-no-policy",
                "採用中の方針がないため、全編へを確定していません。",
            )
        policy_sha = canonical_policy_sha256(policy)
        manifest = _authorized_sample(
            episode_dir,
            consultation_id=consultation_id,
            sample_id=sample_id,
            base_version=base_version,
            base_plan_sha256=base_plan_sha256,
            policy_sha256=policy_sha,
        )
        binding = FullAuthorizationBinding(
            sample_id=manifest.sample_id,
            sample_content_sha256=manifest.content_sha256 or "",
            base_version=base_version,
            base_plan_sha256=base_plan_sha256,
            policy_sha256=policy_sha,
        )
        judgment = _new_full_authorization_judgment(
            consultation_id=consultation_id,
            scope=scope,
            note=note,
            operation_id=operation_id,
            binding=binding,
        )
        append_judgment(episode_dir, judgment)
        return judgment, True


def _new_full_authorization_judgment(
    *,
    consultation_id: str,
    scope: ConsultationScope,
    note: str | None,
    operation_id: str | None,
    binding: FullAuthorizationBinding,
) -> ConsultationJudgmentV1:
    """Build the bound judgment row (server-fixed auth_* fields)."""

    from services.episode_cockpit.consultation_store import (  # noqa: PLC0415 (deferred: store owns the judgment model)
        ConsultationJudgmentV1,
        now_stamp,
    )

    return ConsultationJudgmentV1(
        judgment_id=uuid.uuid4().hex[:12],
        consultation_id=consultation_id,
        proposal_id=None,
        decision="full_authorized",
        scope=scope,
        note=note,
        operation_id=operation_id,
        auth_sample_id=binding.sample_id,
        auth_sample_content_sha256=binding.sample_content_sha256,
        auth_base_version=binding.base_version,
        auth_base_plan_sha256=binding.base_plan_sha256,
        auth_policy_sha256=binding.policy_sha256,
        created_at=now_stamp(),
    )


def full_render_authorized(episode_dir: Path) -> bool:
    """Whether the latest judgment's bound authorization still holds.

    True ONLY when the latest row is ``full_authorized`` AND its
    server-fixed binding matches the CURRENT head version + plan sha,
    the CURRENT adopted-policy sha, and the bound sample's full
    verification (video bytes + recovery boundary). Missing fields
    (legacy unbound rows), a moved head, a changed policy, or a
    missing/tampered sample all fail closed (False). Corrupt judgment
    lines still raise instead of authorizing.
    """

    from services.episode_cockpit.consultation_store import (  # noqa: PLC0415 (deferred: store owns the journal readers)
        canonical_policy_sha256,
        load_judgments,
    )

    judgments = load_judgments(episode_dir)
    if not judgments or judgments[-1].decision != "full_authorized":
        return False
    binding = _full_authorization_binding_of(judgments[-1])
    if binding is None:
        return False
    try:
        base = _current_review_base(episode_dir)
        policy = _strict_policy_before_authorization(episode_dir, judgments)
        return (
            base is not None
            and binding.base_version == base[0]
            and binding.base_plan_sha256 == base[1]
            and policy is not None
            and canonical_policy_sha256(policy) == binding.policy_sha256
            and _bound_sample_intact(episode_dir, binding)
        )
    except Exception:  # noqa: BLE001 (fail-closed authorization; corrupt judgments already raised above)
        return False


__all__ = [
    "FullAuthorizationBinding",
    "append_full_authorization_once",
    "full_authorization_content_key",
    "full_render_authorized",
]
